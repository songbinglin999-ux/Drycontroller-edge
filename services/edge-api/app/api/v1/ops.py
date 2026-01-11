"""
运维控制相关 API。
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.security import require_admin_api_key
from app.services.monitor_config import MonitorConfigService, get_monitor_config_service
from app.services.monitor_ops import MonitorOpsClient, get_monitor_ops_client
from app.services.ota_ops import OtaOpsClient, get_ota_ops_client

# 运维接口必须走管理员鉴权：这些接口包含重启/修改自愈等高危动作
# 注意：这里需要拿到实际 api_key 以便代理调用 edge-monitor 的 /ops/*
router = APIRouter()


class ServiceOverride(BaseModel):
    name: str
    self_heal_enabled: Optional[bool] = None
    failure_threshold: Optional[int] = Field(None, ge=1)
    max_restart_per_hour: Optional[int] = Field(None, ge=0)

    @property
    def as_payload(self) -> dict:
        payload: dict = {}
        if self.self_heal_enabled is not None:
            payload.setdefault("self_heal", {})
            payload["self_heal"]["enabled"] = self.self_heal_enabled
        if self.failure_threshold is not None:
            payload.setdefault("self_heal", {})
            payload["self_heal"]["failure_threshold"] = self.failure_threshold
        if self.max_restart_per_hour is not None:
            payload.setdefault("self_heal", {})
            payload["self_heal"]["max_restart_per_hour"] = self.max_restart_per_hour
        payload["name"] = self.name
        return payload


class MonitorConfigUpdate(BaseModel):
    interval: Optional[int] = Field(None, ge=1, description="服务探活间隔秒")
    http_timeout: Optional[int] = Field(None, ge=1)
    failure_threshold: Optional[int] = Field(None, ge=1)
    recovery_observe: Optional[int] = Field(None, ge=1)
    max_restart_per_hour: Optional[int] = Field(None, ge=0)
    self_heal_enabled: Optional[bool] = None
    targets: Optional[List[ServiceOverride]] = None


class ManualActionRequest(BaseModel):
    service: str


class SelfHealToggleRequest(BaseModel):
    service: str
    enabled: bool


class OtaStartRequest(BaseModel):
    mode: str = Field("online", description="online/offline/offline-tar（MVP 默认 online）")
    target_version: Optional[str] = None
    # 可选：发布侧生成的 docker-compose override（建议使用 digest 固定，避免 tag 漂移）
    # 说明：edge-api 仅做鉴权与透传，实际由 edge-ota-agent 落盘并执行
    compose_override_yaml: Optional[str] = Field(None, description="docker compose override yaml（可选）")
    # 强制验签：若为 true，则要求 override 中的镜像均为 digest 且通过 cosign verify
    cosign_enforce: bool = Field(False, description="是否强制 cosign 验签（推荐逐步开启）")
    dry_run: bool = False
    timeout_sec: int = Field(60 * 30, ge=60, le=60 * 60 * 6)


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
        updates["targets"] = [override.as_payload for override in payload.targets or []]
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

