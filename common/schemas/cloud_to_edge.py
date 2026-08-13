"""
Request bodies for cloud-ops -> edge-api ops endpoints.

These mirror the pydantic models in
Drycontroller-edge/services/edge-api/app/api/v1/ops.py (the receiver side).
Keep field names, types, defaults and Field constraints byte-identical so the
P1-8 hash check stays green.

Source of truth lives on edge-api side; this file is the cloud-ops mirror.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ServiceOverride(BaseModel):
    name: str
    self_heal_enabled: Optional[bool] = None
    failure_threshold: Optional[int] = Field(None, ge=1)
    max_restart_per_hour: Optional[int] = Field(None, ge=0)
    # NOTE: edge-api's previous version of this class had an `as_payload` @property
    # that converted into edge-monitor's internal payload shape. That conversion
    # is edge-api implementation detail (not a wire contract), so it stays
    # private to edge-api's ops.py and is not part of the shared schema.


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
    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(..., pattern=r"^[0-9a-f]{32}$")


class HostReleaseStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(..., pattern=r"^[0-9a-f]{32}$")


class RegistryCredentialRefreshRequest(BaseModel):
    credential_id: str = Field(..., min_length=1, max_length=64)
    registry_host: str = Field(..., min_length=1, max_length=256)
    username: str = Field(..., min_length=1, max_length=256)
    secret: str = Field(..., min_length=1)
    secret_fingerprint: str = Field(..., min_length=1, max_length=64)
    version: int = Field(..., ge=1)


class PlcConfigApplyRequest(BaseModel):
    """
    只做"应用层通讯参数"配置：不修改网卡 IP/掩码/网关（避免共用网口把设备改断联）。
    """

    plc_host: str = Field(..., min_length=1, description="PLC IP/域名（Modbus TCP Server）")
    plc_port: int = Field(502, ge=1, le=65535)
    plc_slave_id: int = Field(1, ge=0, le=255, description="Modbus TCP Unit/Slave ID（部分 PLC 会忽略，但建议与现场一致）")
    plc_connect_timeout: int = Field(5, ge=1, le=60, description="Modbus TCP 连接/请求超时（秒）")
    restart_edge_daq: bool = True
    read_ready_after_restart: bool = True
    fail_on_not_ready: bool = False
