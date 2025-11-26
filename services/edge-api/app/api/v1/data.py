"""
数据查询 API（v1）
"""

from typing import List, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.services.realtime_data import (
    RealtimeDataService,
    get_realtime_data_service,
)

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.get("/realtime")
async def get_realtime_data(
    tags: Optional[List[str]] = Query(None, description="标签列表"),
    lookback: str = Query("5m", description="查询时间窗口，例：5m/1h/1d"),
    service: RealtimeDataService = Depends(get_realtime_data_service),
):
    """
    获取实时数据（等价于 `/api/realtime/latest`）
    """
    try:
        return await service.get_latest_payload(tags, lookback=lookback)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/history")
async def get_history_data(
    tag: str = Query(..., description="标签名称"),
    period: str = Query("1h", description="查询时间范围（例如 1h/6h/1d）"),
    window: str = Query("1m", description="聚合窗口（例如 1m/5m/1h）"),
    aggregation: str = Query(
        "mean",
        description="聚合函数：mean/max/min/sum/last/first",
    ),
    service: RealtimeDataService = Depends(get_realtime_data_service),
):
    """
    查询历史数据
    """
    try:
        return await service.get_history_payload(
            tag=tag,
            period=period,
            window=window,
            aggregation=aggregation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/batch/{batch_id}")
async def get_batch_data(batch_id: str):
    """
    获取生产批次数据（占位接口）
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"Batch data API not implemented yet (batch_id={batch_id})",
    )


@router.get("/tags")
async def get_tags(service: RealtimeDataService = Depends(get_realtime_data_service)):
    """
    获取所有可用标签
    """
    return service.get_tags_payload()

