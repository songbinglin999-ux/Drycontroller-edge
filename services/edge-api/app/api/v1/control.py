"""
控制指令 API（只读推荐）
"""

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.services.control_recommendation import (
    ControlRecommendationService,
    get_control_recommendation_service,
)

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.get("/recommend")
async def get_control_recommendation(
    line: Optional[str] = Query(None, description="可选：按产线过滤"),
    lookback: str = Query("5m", description="分析窗口，如 5m/10m"),
    service: ControlRecommendationService = Depends(get_control_recommendation_service),
):
    """
    获取链速/风机建议（只读），不会直接写 PLC。
    """
    try:
        return await service.get_recommendations(line=line, lookback=lookback)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("/set")
async def set_control():
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Direct control commands are disabled until PLC strategy is approved",
    )


@router.post("/batch")
async def batch_control():
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Batch control is not available",
    )


@router.post("/emergency-stop")
async def emergency_stop():
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Emergency stop must be triggered via PLC workflow",
    )


@router.get("/status")
async def get_control_status():
    return {
        "success": True,
        "data": {
            "mode": "manual",
            "auto_control_enabled": False,
            "emergency_stop_active": False,
            "controllable_tags": ControlRecommendationService.CONTROL_TAGS,
        },
    }

