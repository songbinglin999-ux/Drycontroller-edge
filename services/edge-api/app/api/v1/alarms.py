"""
报警管理 API（只读）
"""

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.services.alarm import AlarmService, get_alarm_service

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.get("/active")
async def get_active_alarms(
    period: str = Query("10m", description="时间范围，例如 10m"),
    severity: Optional[str] = Query(None, description="筛选状态：degraded/down"),
    service: AlarmService = Depends(get_alarm_service),
):
    """
    获取活动报警（基于 system_health measurement）
    """
    return await service.get_active_alarms(period=period, severity=severity)


@router.post("/{alarm_id}/acknowledge")
async def acknowledge_alarm(alarm_id: str):
    """
    确认报警（占位，仅记录日志）
    """
    logger.info("alarm_ack_placeholder", alarm_id=alarm_id)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Alarm acknowledgement is not yet available",
    )


@router.get("/history")
async def get_alarm_history(
    period: str = Query("24h", description="时间范围，例如 24h"),
    severity: Optional[str] = Query(None, description="筛选状态"),
    service: AlarmService = Depends(get_alarm_service),
):
    """
    获取历史报警（只读）
    """
    return await service.get_alarm_history(period=period, severity=severity)

