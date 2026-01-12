from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from common.logging import setup_logging


event_recorder = setup_logging("edge-ota-agent")
logger = structlog.get_logger(__name__)


DATA_DIR = Path(os.getenv("OTA_DATA_DIR", "/data/ota")).resolve()
LOG_DIR = Path(os.getenv("OTA_LOG_DIR", "/logs/edge-ota-agent")).resolve()
WORK_DIR = Path(os.getenv("OTA_WORK_DIR", "/work")).resolve()
COSIGN_PUBKEY_PATH = Path(os.getenv("OTA_COSIGN_PUBKEY_PATH", "/etc/drycontroller/keys/cosign.pub")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _split_api_keys(value: str | None) -> list[str]:
    if not value:
        return []
    # 支持 JSON 数组或逗号分隔（与 edge-api 习惯对齐）
    v = value.strip()
    if v.startswith("["):
        try:
            arr = json.loads(v)
            if isinstance(arr, list):
                return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            pass
    return [item.strip() for item in v.split(",") if item.strip()]


_api_key_header_name = os.getenv("API_KEY_HEADER", "X-API-Key")
_api_key_scheme = APIKeyHeader(name=_api_key_header_name, auto_error=False)


def _get_allowed_api_keys() -> set[str]:
    # 优先使用专用变量；其次复用 ADMIN_API_KEYS（便于与 edge-api/cloud-ops 统一）
    keys = _split_api_keys(os.getenv("OTA_AGENT_API_KEYS"))
    if not keys:
        keys = _split_api_keys(os.getenv("ADMIN_API_KEYS"))
    return set(keys)


async def require_ops_api_key(api_key: str | None = Depends(_api_key_scheme)) -> str:
    allowed = _get_allowed_api_keys()
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ota agent api key security is not configured (set OTA_AGENT_API_KEYS or ADMIN_API_KEYS)",
        )
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
            headers={"WWW-Authenticate": "API-Key"},
        )
    if api_key not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key not permitted")
    return api_key


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _op_path(op_id: str) -> Path:
    return DATA_DIR / f"{op_id}.json"


def _op_log_path(op_id: str) -> Path:
    return LOG_DIR / f"{op_id}.log"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _tail_lines(path: Path, lines: int) -> list[str]:
    if lines <= 0:
        return []
    if not path.exists():
        return []
    # 简单实现：文件不大时读全量；大文件则最多读末尾 512KB
    max_bytes = int(os.getenv("OTA_LOG_TAIL_MAX_BYTES", "524288") or "524288")
    data = path.read_bytes()
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    text = data.decode("utf-8", errors="replace")
    out = text.splitlines()
    return out[-lines:]


class OtaStartRequest(BaseModel):
    mode: str = Field("online", description="online/offline/offline-tar（MVP 默认 online）")
    # 预留：目标版本/发布包信息
    target_version: str | None = None
    # 可选：发布侧生成的 docker-compose override（建议使用 digest 固定）
    compose_override_yaml: str | None = None
    # 强制验签：若为 true，则要求 override 中的镜像均为 digest 且通过 cosign verify
    cosign_enforce: bool = False
    # 预留：仅验证，不执行（MVP 未实现）
    dry_run: bool = False
    # 超时（秒）：避免无限跑挂
    timeout_sec: int = Field(60 * 30, ge=60, le=60 * 60 * 6)  # 30min..6h


class OtaOperationOut(BaseModel):
    operation_id: str
    status: str
    step: str | None = None
    mode: str
    target_version: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    error: str | None = None
    helper_container_id: str | None = None
    helper_image: str | None = None


@dataclass
class AgentRuntime:
    lock: asyncio.Lock


runtime = AgentRuntime(lock=asyncio.Lock())


def _create_operation(payload: OtaStartRequest) -> dict[str, Any]:
    op_id = uuid.uuid4().hex
    op = {
        "operation_id": op_id,
        "status": "queued",
        "step": "queued",
        "mode": payload.mode,
        "target_version": payload.target_version,
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "error": None,
        "helper_container_id": None,
        "helper_image": None,
        "timeout_sec": int(payload.timeout_sec),
    }
    # 可选：把 override 存一份到磁盘（避免状态查询丢失）
    if payload.compose_override_yaml:
        (DATA_DIR / f"{op_id}.compose.override.yml").write_text(payload.compose_override_yaml, encoding="utf-8")
    _write_json(_op_path(op_id), op)
    # 初始化日志文件
    _op_log_path(op_id).write_text("", encoding="utf-8")
    return op


def _update_operation(op_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    p = _op_path(op_id)
    op = _read_json(p)
    op.update(patch)
    _write_json(p, op)
    return op


async def _run_online(op_id: str, payload: OtaStartRequest) -> None:
    """
    在 ota-agent 容器内直接执行脚本（需要容器内具备 bash + docker-compose 客户端）。
    - 该脚本会通过 /var/run/docker.sock 操作宿主机 Docker Engine
    - 该脚本会访问 localhost:${EDGE_API_PORT}/${EDGE_DAQ_HEALTH_PORT} 做 /ready 校验，因此 ota-agent 应与宿主机共享网络：
      - 推荐：docker-compose 侧给 ota-agent 设置 network_mode=host（后续可做更细的网络设计）
      - MVP：你们当前实现用 edge-daq 健康端口绑定 127.0.0.1:8082 来满足校验
    """

    if not (WORK_DIR / "scripts" / "ota-update.sh").exists():
        _update_operation(op_id, {"status": "failed", "step": "init", "error": f"missing_workdir_or_script: {WORK_DIR}"})
        return

    timeout_sec = int(payload.timeout_sec)

    log_path = _op_log_path(op_id)

    _update_operation(
        op_id,
        {
            "status": "running",
            "step": "pull_and_up",
            "started_at": _now().isoformat(),
            "helper_image": None,
            "helper_container_id": None,
        },
    )
    logger.info("ota_started", operation_id=op_id, mode=payload.mode)
    event_recorder.record_event(
        "ota_started",
        tags={"component": "edge-ota-agent"},
        fields={"operation_id": op_id, "mode": payload.mode},
    )

    try:
        cmd = ["bash", "scripts/ota-update.sh", "online", "--yes"]

        # 在容器网络内，默认通过 service name 探测 /ready，而不是 localhost
        env = {**os.environ}
        env.setdefault("EDGE_API_READY_URL", "http://edge-api:8000/ready")
        env.setdefault("EDGE_DAQ_READY_URL", "http://edge-daq:8080/ready")
        # 现场兜底：允许通过环境变量跳过校验（默认不跳过）
        # - 用于 PLC/外设暂时离线但仍需完成“服务升级/回滚”的场景
        if env.get("OTA_SKIP_READY", "").lower() in {"1", "true", "yes"}:
            cmd.append("--skip-ready")
        if env.get("OTA_SKIP_SMOKE", "").lower() in {"1", "true", "yes"}:
            cmd.append("--skip-smoke")
        if env.get("OTA_SKIP_PULL", "").lower() in {"1", "true", "yes"}:
            cmd.append("--skip-pull")

        # 可选：应用 compose override（用于 digest 固定/版本不可漂移）
        if payload.compose_override_yaml:
            override_path = DATA_DIR / f"{op_id}.compose.override.yml"
            if not override_path.exists():
                override_path.write_text(payload.compose_override_yaml, encoding="utf-8")
            base = str(WORK_DIR / "docker-compose.yml")
            env["COMPOSE_FILE"] = f"{base}:{override_path}"

        # ==================== cosign 验签（工业级信任链） ====================
        cosign_enforce = bool(payload.cosign_enforce or (env.get("OTA_COSIGN_ENFORCE", "").lower() in {"1", "true", "yes"}))
        if payload.compose_override_yaml:
            images = _extract_images_from_compose_yaml(payload.compose_override_yaml)
            if cosign_enforce and not images:
                raise RuntimeError("cosign_enforce=true but no images found in compose_override_yaml")
            if images:
                await _cosign_verify_images(op_id, images, enforce=cosign_enforce)
        elif cosign_enforce:
            raise RuntimeError("cosign_enforce=true but compose_override_yaml is missing")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(WORK_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        async def _pump() -> None:
            assert proc.stdout is not None
            with log_path.open("a", encoding="utf-8") as f:
                while True:
                    chunk = await proc.stdout.readline()
                    if not chunk:
                        break
                    s = chunk.decode("utf-8", errors="replace")
                    f.write(s)
                    f.flush()

        pump_task = asyncio.create_task(_pump())
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
        finally:
            await asyncio.gather(pump_task, return_exceptions=True)

        finished = _now().isoformat()
        if code == 0:
            _update_operation(op_id, {"status": "succeeded", "step": "done", "finished_at": finished, "exit_code": code})
            logger.info("ota_succeeded", operation_id=op_id)
            event_recorder.record_event(
                "ota_succeeded",
                tags={"component": "edge-ota-agent"},
                fields={"operation_id": op_id, "exit_code": code},
            )
        else:
            _update_operation(op_id, {"status": "failed", "step": "done", "finished_at": finished, "exit_code": code})
            logger.error("ota_failed", operation_id=op_id, exit_code=code)
            event_recorder.record_event(
                "ota_failed",
                tags={"component": "edge-ota-agent"},
                fields={"operation_id": op_id, "exit_code": code},
            )
    except asyncio.TimeoutError:
        _update_operation(op_id, {"status": "failed", "step": "timeout", "finished_at": _now().isoformat(), "error": "timeout"})
        logger.error("ota_timeout", operation_id=op_id)
    except Exception as exc:
        _update_operation(op_id, {"status": "failed", "step": "error", "finished_at": _now().isoformat(), "error": str(exc)})
        logger.error("ota_exception", operation_id=op_id, error=str(exc))


def _extract_images_from_compose_yaml(yaml_text: str) -> list[str]:
    """
    从 compose yaml（通常为 override）提取 services.*.image。
    """
    try:
        obj = yaml.safe_load(yaml_text) or {}
    except Exception:
        return []
    if not isinstance(obj, dict):
        return []
    services = obj.get("services")
    if not isinstance(services, dict):
        return []
    images: list[str] = []
    for _name, svc in services.items():
        if isinstance(svc, dict) and isinstance(svc.get("image"), str) and svc.get("image").strip():
            images.append(svc["image"].strip())
    # 去重但保序
    seen: set[str] = set()
    out: list[str] = []
    for img in images:
        if img not in seen:
            out.append(img)
            seen.add(img)
    return out


async def _cosign_verify_images(op_id: str, images: list[str], *, enforce: bool) -> None:
    """
    对每个 image 执行：cosign verify --key <pubkey> <image>
    要求：
    - enforce=True 时：必须是 digest 引用（包含 @sha256:）
    - pubkey 必须存在；cosign 必须存在
    """
    log_path = _op_log_path(op_id)

    def _log(line: str) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
            f.flush()

    cosign_bin = shutil.which("cosign")
    if not cosign_bin:
        if enforce:
            raise RuntimeError("cosign binary not found")
        _log("[cosign] WARN: cosign binary not found; skip verification")
        return

    if not COSIGN_PUBKEY_PATH.exists():
        if enforce:
            raise RuntimeError(f"cosign pubkey not found: {COSIGN_PUBKEY_PATH}")
        _log(f"[cosign] WARN: pubkey not found: {COSIGN_PUBKEY_PATH}; skip verification")
        return

    _log(f"[cosign] verify start (enforce={enforce}) pubkey={COSIGN_PUBKEY_PATH}")
    for img in images:
        if enforce and "@sha256:" not in img:
            raise RuntimeError(f"cosign_enforce=true requires digest image reference, got: {img}")

        _log(f"[cosign] verifying {img}")
        proc = await asyncio.create_subprocess_exec(
            cosign_bin,
            "verify",
            "--key",
            str(COSIGN_PUBKEY_PATH),
            img,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out = await proc.stdout.read() if proc.stdout else b""
        text = out.decode("utf-8", errors="replace")
        for line in text.splitlines():
            _log(f"[cosign] {line}")
        code = await proc.wait()
        if code != 0:
            raise RuntimeError(f"cosign verify failed for {img} (exit={code})")
    _log("[cosign] verify ok")


app = FastAPI(title="Edge OTA Agent", version="0.1.0")


@app.get("/health")
async def health():
    return {"success": True, "service": "edge-ota-agent", "timestamp": int(dt.datetime.now().timestamp())}


@app.get("/ready")
async def ready():
    # 简化：工作目录存在且可写即可认为 ready（MVP 不做 docker engine 探测）
    try:
        _ = WORK_DIR.exists()
        (DATA_DIR / ".write_test").write_text("ok", encoding="utf-8")
        return {"ready": True, "service": "edge-ota-agent", "work_dir": str(WORK_DIR)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"ota_agent_not_ready: {exc}")


@app.post("/ota/start", response_model=OtaOperationOut)
async def start_ota(payload: OtaStartRequest, _: str = Depends(require_ops_api_key)):
    if payload.mode not in {"online"}:
        raise HTTPException(status_code=400, detail="only_online_supported_in_mvp")
    if payload.dry_run:
        raise HTTPException(status_code=400, detail="dry_run_not_supported_in_mvp")

    async with runtime.lock:
        op = _create_operation(payload)
        # 后台执行
        asyncio.create_task(_run_online(op["operation_id"], payload))
        return OtaOperationOut(**op)


@app.get("/ota/operations/{operation_id}", response_model=OtaOperationOut)
async def get_operation(operation_id: str, _: str = Depends(require_ops_api_key)):
    try:
        op = _read_json(_op_path(operation_id))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="operation_not_found")
    return OtaOperationOut(**op)


@app.get("/ota/operations/{operation_id}/logs")
async def get_operation_logs(
    operation_id: str,
    lines: int = Query(200, ge=1, le=2000),
    _: str = Depends(require_ops_api_key),
):
    # 不要求 op 一定存在：日志可能在运行中先写入
    data = _tail_lines(_op_log_path(operation_id), lines)
    return {"success": True, "operation_id": operation_id, "count": len(data), "data": data}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "ota_agent.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8090")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )


