"""
运维控制相关 API。
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.services.monitor_config import MonitorConfigService, get_monitor_config_service
from app.services.monitor_ops import MonitorOpsClient, get_monitor_ops_client

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


@router.get("/monitor-config")
async def get_monitor_config(service: MonitorConfigService = Depends(get_monitor_config_service)):
    try:
        return service.load()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="monitor config file not found")


@router.put("/monitor-config")
async def update_monitor_config(
    payload: MonitorConfigUpdate,
    service: MonitorConfigService = Depends(get_monitor_config_service),
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
):
    result = await client.restart_service(req.service)
    return result


@router.post("/actions/self-heal-toggle")
async def manual_toggle_self_heal(
    req: SelfHealToggleRequest,
    client: MonitorOpsClient = Depends(get_monitor_ops_client),
):
    result = await client.toggle_self_heal(req.service, req.enabled)
    return result

