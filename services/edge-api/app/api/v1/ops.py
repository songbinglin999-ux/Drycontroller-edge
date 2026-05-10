"""
运维控制相关 API。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException

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
from app.services.monitor_config import MonitorConfigService, get_monitor_config_service
from app.services.monitor_ops import MonitorOpsClient, get_monitor_ops_client
from app.services.ota_ops import OtaOpsClient, get_ota_ops_client

# 运维接口必须走管理员鉴权：这些接口包含重启/修改自愈等高危动作
# 注意：这里需要拿到实际 api_key 以便代理调用 edge-monitor 的 /ops/*
router = APIRouter()


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

