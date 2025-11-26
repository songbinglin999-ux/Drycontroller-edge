"""
系统管理 API
"""

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.services.system_status import (
    SystemStatusService,
    get_system_status_service,
)

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.get("/status")
async def get_system_status(
    line: Optional[str] = Query(None, description="可选：按产线过滤，如 L1"),
    service: SystemStatusService = Depends(get_system_status_service),
):
    """
    获取系统状态（订阅 edge-monitor）
    """
    payload = service.get_status_payload(line)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No system status available yet",
        )
    return payload


@router.get("/metrics")
async def get_system_metrics(
    line: Optional[str] = Query(None, description="可选：按产线过滤"),
    service: SystemStatusService = Depends(get_system_status_service),
):
    """
    获取系统性能指标
    """
    payload = service.get_metrics_payload(line)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No system metrics available yet",
        )
    return payload


@router.get("/metrics/trend")
async def get_system_metric_trend(
    field: str = Query("cpu_percent", description="指标字段，例如 cpu_percent"),
    period: str = Query("6h", description="时间范围，例如 6h/12h/1d"),
    window: str = Query("5m", description="聚合窗口，例如 1m/5m/1h"),
    line: Optional[str] = Query(None, description="可选：按产线过滤"),
    service: SystemStatusService = Depends(get_system_status_service),
):
    """
    获取系统指标趋势（来自 Influx 聚合）
    """
    try:
        return await service.get_metric_trend(
            field=field,
            period=period,
            window=window,
            line=line,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/config")
async def get_system_config():
    """
    系统配置（占位）
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="System config API not implemented yet",
    )


@router.post("/config")
async def update_system_config():
    """
    更新配置（占位）
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="System config update API not implemented yet",
    )


@router.get("/logs")
async def get_system_logs():
    """
    系统日志（占位）
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="System logs API not implemented yet",
    )

