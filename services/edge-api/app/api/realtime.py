"""
实时数据 API
"""

import asyncio
import time
from typing import List, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi import WebSocket, WebSocketDisconnect

from app.services.influx import InfluxQueryService, get_influx_service
from app.utils.tag_loader import get_all_tag_names

router = APIRouter()
logger = structlog.get_logger(__name__)


def _resolve_tags(requested: Optional[List[str]]) -> List[str]:
    if requested:
        return [tag.strip() for tag in requested if tag.strip()]
    return get_all_tag_names()


@router.get("/realtime/latest")
async def realtime_latest(
    tags: Optional[List[str]] = Query(
        None,
        description="指定标签列表，不传则返回全部配置标签",
    ),
    lookback: str = Query(
        "5m",
        description="查询时间窗口，例如 5m/1h/1d",
    ),
    influx: InfluxQueryService = Depends(get_influx_service),
):
    tag_list = _resolve_tags(tags)
    if not tag_list:
        return {"success": True, "data": [], "timestamp": int(time.time())}

    try:
        points = await influx.latest_points(tag_list, lookback=lookback)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    payload_tags = {point["tag"] for point in points}
    missing = sorted(set(tag_list) - payload_tags)

    return {
        "success": True,
        "data": points,
        "missing": missing,
        "count": len(points),
        "timestamp": int(time.time()),
    }


@router.get("/history")
async def history(
    tag: str = Query(..., description="标签名称"),
    period: str = Query("1h", description="时间范围，例如 1h/6h/1d"),
    window: str = Query("1m", description="聚合窗口，例如 1m/5m/1h"),
    aggregation: str = Query(
        "mean",
        description="聚合函数，可选 mean/max/min/sum/last/first",
    ),
    influx: InfluxQueryService = Depends(get_influx_service),
):
    try:
        values = await influx.history_points(
            tag=tag,
            period=period,
            window=window,
            aggregation=aggregation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return {
        "success": True,
        "data": {
            "tag": tag,
            "points": values,
        },
        "count": len(values),
        "timestamp": int(time.time()),
    }


@router.websocket("/realtime/stream")
async def realtime_stream(websocket: WebSocket):
    await websocket.accept()
    params = websocket.query_params
    tags_param = params.get("tags")
    lookback = params.get("lookback", "5m")
    interval = params.get("interval", "1.0")

    tags: Optional[List[str]] = None
    if tags_param:
        tags = [item.strip() for item in tags_param.split(",") if item.strip()]

    try:
        interval_seconds = max(0.5, float(interval))
    except ValueError:
        interval_seconds = 1.0

    influx = get_influx_service()
    logger.info("realtime_stream_connected", tags=tags, interval=interval_seconds)

    try:
        while True:
            tag_list = _resolve_tags(tags)
            data = await influx.latest_points(tag_list, lookback=lookback)
            await websocket.send_json(
                {
                    "type": "realtime",
                    "tags": tag_list,
                    "data": data,
                    "timestamp": int(time.time()),
                }
            )
            await asyncio.sleep(interval_seconds)
    except WebSocketDisconnect:
        logger.info("realtime_stream_disconnected")
    except Exception as exc:
        logger.exception("realtime_stream_error", error=str(exc))
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)

