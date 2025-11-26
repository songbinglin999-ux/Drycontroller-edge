"""
AI 相关 API
"""

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.services.ai_softsensor import (
    AISoftsensorService,
    get_ai_softsensor_service,
)

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.get("/moisture/current")
async def get_current_moisture(
    line: Optional[str] = Query(None, description="可选：按产线过滤，如 L1"),
    service: AISoftsensorService = Depends(get_ai_softsensor_service),
):
    """
    获取当前含水率预测（来自 ai-softsensor 实时流）
    """
    payload = service.get_current_moisture(line)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No AI moisture prediction available yet",
        )
    return payload


@router.get("/moisture/history")
async def get_moisture_history(
    period: str = Query("1d", description="时间范围，例如 6h/12h/1d"),
    window: str = Query("5m", description="聚合窗口，例如 1m/5m/1h"),
    aggregation: str = Query(
        "mean",
        description="聚合函数：mean/max/min/sum/last/first",
    ),
    service: AISoftsensorService = Depends(get_ai_softsensor_service),
):
    """
    查询历史含水率预测（来自 Influx）
    """
    try:
        return await service.get_moisture_history(
            period=period,
            window=window,
            aggregation=aggregation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/lab/samples")
async def get_lab_samples(
    start: str = Query("-7d", description="Flux range start，例如 -7d"),
    stop: str = Query("now()", description="Flux range stop，默认 now()"),
    line: Optional[str] = Query(None, description="可选：按产线过滤"),
    require_lab: bool = Query(True, description="仅返回已录入 LAB 的样本"),
    limit: int = Query(200, ge=1, le=2000, description="最多返回的数据条数"),
    service: AISoftsensorService = Depends(get_ai_softsensor_service),
):
    """
    查询 softsensor 训练数据中的样本（便于导出训练集或对齐 LAB）
    """
    return await service.get_lab_samples(
        start=start,
        stop=stop,
        line=line,
        require_lab=require_lab,
        limit=limit,
    )


@router.get("/optimization/recommendation")
async def get_optimization_recommendation():
    """
    控制优化建议（占位）
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Optimizer recommendation API not implemented yet",
    )


@router.post("/optimization/apply")
async def apply_optimization():
    """
    应用优化建议（占位）
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Optimization apply API not implemented yet",
    )


@router.get("/models")
async def get_models(service: AISoftsensorService = Depends(get_ai_softsensor_service)):
    """
    获取 ai-config.yaml 中的软测量/优化器模型信息
    """
    payload = service.get_models_info()
    if not payload.get("data"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="AI config not available",
        )
    return payload

