"""
系统管理 API
"""

from typing import Optional

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.config import settings
from app.core.security import require_admin_api_key
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
async def get_system_config(
    _: str = Depends(require_admin_api_key),
):
    """
    系统配置（占位）
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="System config API not implemented yet",
    )


@router.post("/config")
async def update_system_config(
    _: str = Depends(require_admin_api_key),
):
    """
    更新配置（占位）
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="System config update API not implemented yet",
    )


@router.get("/container/logs")
async def get_container_logs(
    container: str = Query(..., description="容器名称"),
    lines: int = Query(200, description="日志行数", ge=1, le=2000),
    api_key: str = Depends(require_admin_api_key),
):
    """
    获取容器日志（代理 edge-monitor）
    """
    monitor_base = settings.MONITOR_API_BASE.rstrip("/")
    url = f"{monitor_base}/ops/logs/{container}"
    
    headers = {settings.API_KEY_HEADER: api_key} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, params={"lines": lines}, headers=headers)
    except httpx.RequestError as exc:
        logger.error("monitor_log_proxy_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="edge-monitor unreachable",
        )
    
    if response.status_code >= 400:
        detail = None
        if response.headers.get("content-type", "").startswith("application/json"):
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
        if not detail:
            detail = response.text
        raise HTTPException(status_code=response.status_code, detail=detail or "Failed to fetch logs")
    
    try:
        payload = response.json()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="edge-monitor returned invalid payload",
        )
    if "success" not in payload:
        payload = {"success": True, "data": payload}
    return payload

