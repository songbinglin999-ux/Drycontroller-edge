"""
控制指令 API（只读推荐）
"""

from typing import Any, List, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.security import require_control_api_key
from app.services.control_recommendation import (
    ControlRecommendationService,
    get_control_recommendation_service,
)
from app.services.control_command import ControlCommandService, get_control_command_service

router = APIRouter(dependencies=[Depends(require_control_api_key)])
logger = structlog.get_logger(__name__)


@router.get("/recommend")
async def get_control_recommendation(
    line: Optional[str] = Query(None, description="可选：按产线过滤"),
    lookback: str = Query("5m", description="分析窗口，如 5m/10m"),
    service: ControlRecommendationService = Depends(get_control_recommendation_service),
):
    """
    获取规则诊断与只读建议，不会直接写 PLC。
    """
    try:
        return await service.get_recommendations(line=line, lookback=lookback)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


class ControlSetRequest(BaseModel):
    line: Optional[str] = Field(None, description="产线标识，默认 L1")
    tag: str = Field(..., description="PLC 可控标签（需在 tags.yaml 标注 control_enabled=true）")
    value: Any = Field(..., description="写入值（数值/布尔等）")
    id: Optional[str] = Field(None, description="幂等 ID（可选，留空由服务端生成）")
    wait_ack: bool = Field(True, description="是否等待 edge-daq ACK（闭环）")
    timeout_sec: float = Field(3.0, ge=0.2, le=30.0, description="等待 ACK 超时（秒）")


@router.post("/set")
async def set_control(
    req: ControlSetRequest,
    svc: ControlCommandService = Depends(get_control_command_service),
):
    line = req.line or "L1"
    resp = await svc.send_set(
        line=line,
        tag=req.tag,
        value=req.value,
        msg_id=req.id,
        wait_ack=req.wait_ack,
        timeout_sec=req.timeout_sec,
    )
    if not resp.get("success"):
        # ack_timeout -> 504；其他 -> 400/503
        if resp.get("error") == "mqtt_not_connected":
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MQTT not connected")
        if resp.get("error") == "ack_timeout":
            raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Control ACK timeout")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=resp.get("error") or "control_failed")
    return resp


class ControlBatchItem(BaseModel):
    tag: str = Field(..., description="标签")
    value: Any = Field(..., description="值")
    id: Optional[str] = Field(None, description="幂等 ID（可选）")


class ControlBatchRequest(BaseModel):
    line: Optional[str] = Field(None, description="产线标识，默认 L1")
    items: List[ControlBatchItem] = Field(..., min_length=1, description="批量写入项")
    wait_ack: bool = Field(True, description="是否等待 ACK（闭环）")
    timeout_sec: float = Field(5.0, ge=0.2, le=60.0, description="每条 ACK 超时（秒）")


@router.post("/batch")
async def batch_control(
    req: ControlBatchRequest,
    svc: ControlCommandService = Depends(get_control_command_service),
):
    line = req.line or "L1"
    resp = await svc.send_batch(
        line=line,
        items=[item.model_dump() for item in req.items],
        wait_ack=req.wait_ack,
        timeout_sec=req.timeout_sec,
    )
    if not resp.get("success"):
        # 批量：部分失败也返回 200，但 success=false
        return resp
    return resp


class EmergencyStopRequest(BaseModel):
    line: Optional[str] = Field(None, description="产线标识，默认 L1")
    wait_ack: bool = Field(True, description="是否等待 ACK（闭环）")
    timeout_sec: float = Field(6.0, ge=0.2, le=120.0, description="等待 ACK 超时（秒）")


@router.post("/emergency-stop")
async def emergency_stop(
    req: EmergencyStopRequest,
    svc: ControlCommandService = Depends(get_control_command_service),
):
    line = req.line or "L1"
    resp = await svc.emergency_stop(line=line, wait_ack=req.wait_ack, timeout_sec=req.timeout_sec)
    if not resp.get("success"):
        if resp.get("error") == "mqtt_not_connected":
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MQTT not connected")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=resp.get("error") or "emergency_stop_failed")
    return resp


@router.get("/status")
async def get_control_status():
    return {
        "success": True,
        "data": {
            "mode": "manual",
            "auto_control_enabled": False,
            "emergency_stop_active": False,
            "controllable_tags": ControlRecommendationService.CONTROL_SOFTPOINTS,
            "control_softpoints_status": "tbd",
            "suggestion_schema": ControlRecommendationService.SCHEMA_VERSION,
            "actionability": "not_actionable",
            "blocked_by": ControlRecommendationService.BLOCKED_BY,
        },
    }
