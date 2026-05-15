"""
运维控制相关 API。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from typing import Any
from pathlib import Path

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

# 协议 schema 已移到仓库根 common/schemas/cloud_to_edge.py（P1-8 phase B），
# 与 cloud-ops 仓库的同名文件保持 byte-identical 同步，CI hash 校验保障一致性。
# 详见 Drycontroller-cloudserver/docs/architecture/shared-schemas.md。
from common.schemas.cloud_to_edge import (
    ManualActionRequest,
    MonitorConfigUpdate,
    OtaStartRequest,
    PlcConfigApplyRequest,
    SelfHealToggleRequest,
    ServiceOverride,
)

from app.core.security import require_admin_api_key
from app.core.config import settings
from app.services.monitor_config import MonitorConfigService, get_monitor_config_service
from app.services.monitor_ops import MonitorOpsClient, get_monitor_ops_client
from app.services.ota_ops import OtaOpsClient, get_ota_ops_client
from common.license_verifier import load_public_key, verify_signature

# 运维接口必须走管理员鉴权：这些接口包含重启/修改自愈等高危动作
# 注意：这里需要拿到实际 api_key 以便代理调用 edge-monitor 的 /ops/*
router = APIRouter()


class LicenseInstallRequest(BaseModel):
    """
    云端签发后的 License 安装请求。

    现场人员不应手工拷贝文件；edge-api 接收云端下发后负责落盘、校验、失败回滚。
    """

    license: dict[str, Any] = Field(..., description="完整 signed license JSON（扁平结构，包含 signature）")
    license_public_key: str = Field(..., min_length=1, description="base64 编码 Ed25519 公钥")


class PreflightRequest(BaseModel):
    """
    云端触发的边端只读出厂检查。

    该接口不执行宿主机 shell 脚本；它在 edge-api 容器内做等价的远程可观测检查，
    保证 cloud-ops 可以无人值守触发并拿到结构化结果。
    """

    strict: bool = Field(False, description="true 时 degraded 也视为失败")
    check_ota: bool = Field(False, description="true 时要求 edge-ota-agent /ready 可用")


def _service_override_payload(o: ServiceOverride) -> dict:
    """
    把共享 schema ServiceOverride 转换为 edge-monitor 内部 monitor_config 的
    payload 形状。这是 edge-api 私有的实现细节（不是协议契约的一部分），
    所以不放进 common/schemas/。
    """
    payload: dict = {}
    if o.self_heal_enabled is not None:
        payload.setdefault("self_heal", {})
        payload["self_heal"]["enabled"] = o.self_heal_enabled
    if o.failure_threshold is not None:
        payload.setdefault("self_heal", {})
        payload["self_heal"]["failure_threshold"] = o.failure_threshold
    if o.max_restart_per_hour is not None:
        payload.setdefault("self_heal", {})
        payload["self_heal"]["max_restart_per_hour"] = o.max_restart_per_hour
    payload["name"] = o.name
    return payload


def _atomic_write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{int(time.time())}")
    with tmp.open("w", encoding="utf-8") as fp:
        yaml.safe_dump(data, fp, allow_unicode=True, sort_keys=False)
    tmp.replace(path)


def _write_bytes_atomic(path: Path, data: bytes, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{int(time.time() * 1000)}")
    try:
        with tmp.open("wb") as fp:
            fp.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _restore_or_remove(path: Path, old_data: bytes | None, mode: int = 0o640) -> None:
    if old_data is None:
        path.unlink(missing_ok=True)
        return
    _write_bytes_atomic(path, old_data, mode=mode)


def _validate_license_install_shape(req: LicenseInstallRequest) -> None:
    required_fields = {"signature", "signature_alg", "license_id", "machine_id", "issued_at", "expires_at"}
    missing = sorted(required_fields - set(req.license.keys()))
    if missing:
        raise HTTPException(status_code=400, detail={"success": False, "error": "license_missing_fields", "missing": missing})
    if str(req.license.get("signature_alg") or "").lower() != "ed25519":
        raise HTTPException(status_code=400, detail={"success": False, "error": "unsupported_signature_alg"})
    for field in ("signature", "license_id", "machine_id", "issued_at", "expires_at"):
        if not str(req.license.get(field) or "").strip():
            raise HTTPException(status_code=400, detail={"success": False, "error": "license_payload_missing_field", "field": field})
    try:
        base64.b64decode(req.license_public_key.strip(), validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail={"success": False, "error": "invalid_license_public_key_base64"})


def _trusted_license_public_key(public_key_path: Path) -> bytes:
    configured = str(settings.LICENSE_TRUSTED_PUBLIC_KEY or "").strip()
    if configured:
        try:
            return base64.b64decode(configured, validate=True)
        except Exception:
            raise HTTPException(status_code=500, detail={"success": False, "error": "invalid_trusted_license_public_key"})
    if public_key_path.is_file():
        try:
            return load_public_key(public_key_path)
        except Exception:
            raise HTTPException(status_code=500, detail={"success": False, "error": "invalid_existing_license_public_key"})
    raise HTTPException(status_code=409, detail={"success": False, "error": "missing_trusted_license_public_key"})


def _verify_license_install_request(req: LicenseInstallRequest, *, public_key_path: Path) -> bytes:
    request_public_key = base64.b64decode(req.license_public_key.strip(), validate=True)
    trusted_public_key = _trusted_license_public_key(public_key_path)
    if request_public_key != trusted_public_key:
        raise HTTPException(status_code=403, detail={"success": False, "error": "license_public_key_not_trusted"})
    try:
        verify_signature(req.license, trusted_public_key)
    except Exception as exc:
        raise HTTPException(status_code=400, detail={"success": False, "error": "license_signature_invalid"}) from exc
    return trusted_public_key


async def _call_license_verify() -> dict:
    url = f"{settings.LICENSE_SERVICE_BASE.rstrip('/')}/verify"
    try:
        async with httpx.AsyncClient(timeout=8) as hc:
            resp = await hc.get(url)
        body = resp.json() if resp.content else {}
        return {"url": url, "status_code": resp.status_code, "body": body}
    except Exception as exc:
        return {"url": url, "status_code": 0, "error": str(exc)}


def _license_verify_ok(result: dict) -> bool:
    body = result.get("body")
    return int(result.get("status_code") or 0) == 200 and isinstance(body, dict) and body.get("valid") is True


def _preflight_check(checks: list[dict[str, Any]], check_id: str, status: str, reason: str, hint: str = "", detail: dict[str, Any] | None = None) -> None:
    checks.append(
        {
            "id": check_id,
            "status": status,
            "reason": reason,
            "hint": hint,
            "detail": detail or {},
        }
    )


def _preflight_file_check(checks: list[dict[str, Any]], check_id: str, path: Path, *, hint: str) -> None:
    if path.is_file():
        _preflight_check(checks, check_id, "pass", "ok", detail={"path": str(path)})
    else:
        _preflight_check(checks, check_id, "fail", "missing_file", hint=hint, detail={"path": str(path)})


def _preflight_device_identity(checks: list[dict[str, Any]]) -> None:
    path = Path(os.getenv("PREFLIGHT_DEVICE_JSON", "/var/lib/drycontroller/device.json"))
    if not path.is_file():
        _preflight_check(
            checks,
            "device.identity",
            "fail",
            "missing_device_json",
            hint=f"bootstrap should create {path}",
            detail={"path": str(path)},
        )
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _preflight_check(checks, "device.identity", "fail", "invalid_json", hint="device.json must be valid JSON", detail={"path": str(path), "error": str(exc)})
        return
    missing = [k for k in ("device_id", "tenant_id", "node_name") if not data.get(k)]
    _preflight_check(
        checks,
        "device.identity",
        "fail" if missing else "pass",
        "missing_required_fields" if missing else "ok",
        hint="device.json must contain device_id/tenant_id/node_name" if missing else "",
        detail={
            "path": str(path),
            "missing": missing,
            "device_id": data.get("device_id"),
            "tenant_id": data.get("tenant_id"),
            "node_name": data.get("node_name"),
        },
    )


async def _preflight_http_check(
    checks: list[dict[str, Any]],
    check_id: str,
    url: str,
    *,
    degraded_ok: bool,
    allow_degraded: bool,
    timeout: float = 5.0,
) -> None:
    try:
        async with httpx.AsyncClient(timeout=timeout) as hc:
            resp = await hc.get(url)
        body: Any
        try:
            body = resp.json() if resp.content else None
        except Exception:
            body = resp.text[:800]
        detail = {"url": url, "status_code": resp.status_code, "body": body}
        if 200 <= resp.status_code < 300:
            _preflight_check(checks, check_id, "pass", "ok", detail=detail)
        elif resp.status_code == 503 and degraded_ok and allow_degraded:
            _preflight_check(checks, check_id, "degraded", "service_degraded", hint="accepted in factory mode; use strict for final acceptance", detail=detail)
        else:
            _preflight_check(checks, check_id, "fail", f"http_{resp.status_code}", hint="unexpected readiness status", detail=detail)
    except Exception as exc:
        _preflight_check(checks, check_id, "fail", "unreachable", hint="endpoint is not reachable", detail={"url": url, "error": str(exc)})


def _preflight_summary(checks: list[dict[str, Any]], *, strict: bool) -> dict[str, Any]:
    counts = {"pass": 0, "degraded": 0, "fail": 0}
    for check in checks:
        status_text = str(check.get("status") or "")
        if status_text in counts:
            counts[status_text] += 1
    status_text = "fail" if counts["fail"] or (strict and counts["degraded"]) else ("degraded" if counts["degraded"] else "pass")
    return {
        "success": status_text != "fail",
        "status": status_text,
        "summary": counts,
    }


@router.get("/alerts")
async def list_alerts(
    severity: str | None = None,
    service: str | None = None,
    state: str | None = "active",
    client: MonitorOpsClient = Depends(get_monitor_ops_client),
    api_key: str = Depends(require_admin_api_key),
):
    """
    代理 edge-monitor 的 /ops/alerts，供前端展示告警。
    """
    return await client.list_alerts(severity=severity, service=service, state=state, api_key=api_key)


# ==================== License 安装与校验（云端决策，边端无人值守执行） ====================


@router.get("/license/verify")
async def verify_license(_: str = Depends(require_admin_api_key)):
    """
    代理 license-service /verify，供云端安装前后读取边端真实验签状态。
    """
    result = await _call_license_verify()
    return {
        "success": _license_verify_ok(result),
        "verify": result,
    }


@router.post("/preflight")
async def run_preflight(req: PreflightRequest, _: str = Depends(require_admin_api_key)):
    """
    云端触发的只读 preflight。

    本地工厂位仍可直接运行 scripts/preflight.sh；云端 job 使用本接口拿结构化结果。
    """
    checks: list[dict[str, Any]] = []
    allow_degraded = not req.strict

    _preflight_file_check(
        checks,
        "bootstrap.sentinel",
        Path(os.getenv("PREFLIGHT_BOOTSTRAP_SENTINEL", "/var/lib/drycontroller/.bootstrapped")),
        hint="run USB/bootstrap provisioning before shipping",
    )
    _preflight_device_identity(checks)
    _preflight_file_check(
        checks,
        "license.file",
        Path(settings.LICENSE_FILE_PATH),
        hint="install signed license before shipping",
    )
    _preflight_file_check(
        checks,
        "license.public_key",
        Path(settings.LICENSE_PUBLIC_KEY_PATH),
        hint="install license public key before shipping",
    )

    verify_result = await _call_license_verify()
    _preflight_check(
        checks,
        "license.verify",
        "pass" if _license_verify_ok(verify_result) else "fail",
        "ok" if _license_verify_ok(verify_result) else "license_verify_failed",
        hint="license-service /verify must return valid=true",
        detail=verify_result,
    )

    _preflight_check(checks, "edge_api.ready", "pass", "ok", detail={"source": "preflight endpoint reached"})
    await _preflight_http_check(
        checks,
        "edge_daq.ready",
        "http://edge-daq:8080/ready",
        degraded_ok=True,
        allow_degraded=allow_degraded,
    )
    await _preflight_http_check(
        checks,
        "edge_monitor.ready",
        f"{settings.MONITOR_API_BASE.rstrip('/')}/ready",
        degraded_ok=False,
        allow_degraded=allow_degraded,
    )
    if req.check_ota:
        await _preflight_http_check(
            checks,
            "ota_agent.ready",
            f"{settings.OTA_AGENT_API_BASE.rstrip('/')}/ready",
            degraded_ok=False,
            allow_degraded=allow_degraded,
        )
    else:
        _preflight_check(
            checks,
            "ota_agent.ready",
            "degraded" if allow_degraded else "fail",
            "skipped",
            hint="edge-ota-agent check skipped; set check_ota=true for final OTA acceptance",
        )

    ts_login_server = os.getenv("TS_LOGIN_SERVER", "").strip()
    ts_hostname = os.getenv("TS_HOSTNAME", "").strip()
    _preflight_check(
        checks,
        "tailnet.identity",
        "pass" if ts_login_server else ("degraded" if allow_degraded else "fail"),
        "configured" if ts_login_server else "missing_tailnet_config",
        hint="configure TS_LOGIN_SERVER/TS_HOSTNAME for remote operations",
        detail={"ts_login_server_present": bool(ts_login_server), "ts_hostname": ts_hostname or None},
    )

    cloud_ops_base_url = os.getenv("CLOUD_OPS_BASE_URL", "").strip()
    enroll_token = os.getenv("CLOUD_OPS_ENROLL_TOKEN", "").strip()
    _preflight_check(
        checks,
        "cloud.enroll.config",
        "pass" if cloud_ops_base_url and enroll_token else "fail",
        "configured" if cloud_ops_base_url and enroll_token else "missing_cloud_enroll_config",
        hint="configure CLOUD_OPS_BASE_URL and CLOUD_OPS_ENROLL_TOKEN",
        detail={"cloud_ops_base_url_present": bool(cloud_ops_base_url), "enroll_token_present": bool(enroll_token)},
    )

    summary = _preflight_summary(checks, strict=req.strict)
    payload = {
        **summary,
        "checks": checks,
        "strict": req.strict,
        "check_ota": req.check_ota,
        "generated_at": int(time.time()),
    }
    if not payload["success"]:
        raise HTTPException(status_code=409, detail=payload)
    return payload


@router.post("/license/install")
async def install_license(req: LicenseInstallRequest, _: str = Depends(require_admin_api_key)):
    """
    安装云端签发的 License。

    关键保证：
    - 只写固定 license 文件与公钥文件，不触碰控制/采集服务；
    - 文件替换使用 os.replace，避免半写入；
    - 新 License 验签失败时恢复旧文件，现场运行不因失败安装而丢失旧授权。
    """
    _validate_license_install_shape(req)

    license_path = Path(settings.LICENSE_FILE_PATH)
    public_key_path = Path(settings.LICENSE_PUBLIC_KEY_PATH)
    _verify_license_install_request(req, public_key_path=public_key_path)
    old_license = license_path.read_bytes() if license_path.exists() else None
    old_public_key = public_key_path.read_bytes() if public_key_path.exists() else None

    license_bytes = json.dumps(req.license, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    public_key_bytes = req.license_public_key.strip().encode("utf-8") + b"\n"

    _write_bytes_atomic(license_path, license_bytes, mode=0o640)
    _write_bytes_atomic(public_key_path, public_key_bytes, mode=0o640)

    verify_result = await _call_license_verify()
    if _license_verify_ok(verify_result):
        return {
            "success": True,
            "license_path": str(license_path),
            "public_key_path": str(public_key_path),
            "verify": verify_result,
            "restart_required": False,
        }

    _restore_or_remove(license_path, old_license, mode=0o640)
    _restore_or_remove(public_key_path, old_public_key, mode=0o640)
    rollback_verify = await _call_license_verify()
    raise HTTPException(
        status_code=409,
        detail={
            "success": False,
            "error": "license_verify_failed_after_install",
            "verify": verify_result,
            "rollback_verify": rollback_verify,
        },
    )


@router.get("/monitor-config")
async def get_monitor_config(
    service: MonitorConfigService = Depends(get_monitor_config_service),
    _: str = Depends(require_admin_api_key),
):
    try:
        return service.load()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="monitor config file not found")


@router.put("/monitor-config")
async def update_monitor_config(
    payload: MonitorConfigUpdate,
    service: MonitorConfigService = Depends(get_monitor_config_service),
    _: str = Depends(require_admin_api_key),
):
    updates = payload.model_dump(exclude_none=True)
    if "targets" in updates:
        updates["targets"] = [_service_override_payload(o) for o in payload.targets or []]
    patched = service.patch(updates)
    return {"success": True, "config": patched}


@router.post("/actions/restart")
async def manual_restart(
    req: ManualActionRequest,
    client: MonitorOpsClient = Depends(get_monitor_ops_client),
    api_key: str = Depends(require_admin_api_key),
):
    result = await client.restart_service(req.service, api_key=api_key)
    return result


@router.post("/actions/self-heal-toggle")
async def manual_toggle_self_heal(
    req: SelfHealToggleRequest,
    client: MonitorOpsClient = Depends(get_monitor_ops_client),
    api_key: str = Depends(require_admin_api_key),
):
    result = await client.toggle_self_heal(req.service, req.enabled, api_key=api_key)
    return result


# ==================== OTA（由 edge-ota-agent 执行，edge-api 负责鉴权与代理） ====================


@router.post("/ota/start")
async def ota_start(
    req: OtaStartRequest,
    client: OtaOpsClient = Depends(get_ota_ops_client),
    api_key: str = Depends(require_admin_api_key),
):
    # 透传到 ota-agent（MVP 仅支持 online）
    return await client.start(api_key=api_key, payload=req.model_dump(exclude_none=True))


@router.get("/ota/operations/{operation_id}")
async def ota_get_operation(
    operation_id: str,
    client: OtaOpsClient = Depends(get_ota_ops_client),
    api_key: str = Depends(require_admin_api_key),
):
    return await client.get_operation(operation_id, api_key=api_key)


@router.get("/ota/operations/{operation_id}/logs")
async def ota_get_logs(
    operation_id: str,
    lines: int = 200,
    client: OtaOpsClient = Depends(get_ota_ops_client),
    api_key: str = Depends(require_admin_api_key),
):
    return await client.get_logs(operation_id, api_key=api_key, lines=lines)


# ==================== PLC 通讯参数下发（不改网卡，仅改应用层） ====================


@router.post("/plc-config/apply")
async def apply_plc_config(
    req: PlcConfigApplyRequest,
    client: MonitorOpsClient = Depends(get_monitor_ops_client),
    api_key: str = Depends(require_admin_api_key),
):
    """
    云端下发 PLC 通讯参数：
    1) 写入 /app/configs-runtime/plc.yaml（bind mount 到宿主机 ./configs-runtime）
    2) 可选：通过 edge-monitor 重启 edge-daq 使其加载新配置
    3) 可选：读取 edge-daq /ready 返回（用于云端展示 PLC_OFFLINE 等原因）
    """
    if req.plc_host.strip() in {"127.0.0.1", "localhost"}:
        # 工业现场必坑：容器内 127.0.0.1 指向自身，必然连不上 PLC
        raise HTTPException(status_code=400, detail="PLC_HOST cannot be 127.0.0.1/localhost in docker; use real PLC IP")

    cfg_path = Path("/app/configs-runtime/plc.yaml")
    payload = {
        "host": req.plc_host.strip(),
        "port": int(req.plc_port),
        "slave_id": int(req.plc_slave_id),
        "connect_timeout": int(req.plc_connect_timeout),
        "updated_at": int(time.time()),
        "updated_by": "cloud",
    }
    _atomic_write_yaml(cfg_path, payload)

    restart_result = None
    if req.restart_edge_daq:
        restart_result = await client.restart_service("edge-daq", api_key=api_key)

    ready_result: dict | None = None
    if req.read_ready_after_restart:
        # 给容器一点启动时间（避免刚重启就探测）
        await asyncio.sleep(2)
        url = "http://edge-daq:8080/ready"
        try:
            async with httpx.AsyncClient(timeout=6) as hc:
                resp = await hc.get(url)
                ready_result = {
                    "url": url,
                    "status_code": resp.status_code,
                    "json": resp.json() if resp.content else None,
                    "text": (resp.text[:1000] if resp.text else None),
                }
        except Exception as exc:
            ready_result = {"url": url, "status_code": 0, "error": str(exc)}

    if req.fail_on_not_ready and ready_result and int(ready_result.get("status_code") or 0) >= 400:
        raise HTTPException(status_code=409, detail={"success": False, "error": "edge_daq_not_ready", "ready": ready_result})

    return {
        "success": True,
        "config_path": str(cfg_path),
        "config": payload,
        "restart": restart_result,
        "edge_daq_ready": ready_result,
    }
