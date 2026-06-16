"""
实时数据 API
"""

import time
from typing import Callable, List, Optional, Set

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi import WebSocket, WebSocketDisconnect

from app.services.ws_stream import ws_stream
from app.services.realtime_data import (
    RealtimeDataService,
    get_realtime_data_service,
)
from app.core.security import require_readonly_http_source, require_readonly_ws_source

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.get("/realtime/latest", dependencies=[Depends(require_readonly_http_source)])
async def realtime_latest(
    tags: Optional[List[str]] = Query(
        None,
        description="指定标签列表，不传则返回全部配置标签",
    ),
    lookback: str = Query(
        "5m",
        description="查询时间窗口，例如 5m/1h/1d",
    ),
    service: RealtimeDataService = Depends(get_realtime_data_service),
):
    try:
        return await service.get_latest_payload(tags, lookback=lookback)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/history", dependencies=[Depends(require_readonly_http_source)])
async def history(
    tag: str = Query(..., description="标签名称"),
    period: str = Query("1h", description="时间范围，例如 1h/6h/1d"),
    window: str = Query("1m", description="聚合窗口，例如 1m/5m/1h"),
    aggregation: str = Query(
        "mean",
        description="聚合函数，可选 mean/max/min/sum/last/first",
    ),
    service: RealtimeDataService = Depends(get_realtime_data_service),
):
    try:
        return await service.get_history_payload(
            tag=tag,
            period=period,
            window=window,
            aggregation=aggregation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# =========== WebSocket MQTT Streaming ===========


def _build_realtime_predicate(line: Optional[str], tags: Optional[Set[str]]) -> Optional[Callable[[dict], bool]]:
    if not line and not tags:
        return None
    
    def predicate(message: dict) -> bool:
        if line and message.get("line") != line:
            return False
        if tags and message.get("tag") not in tags:
            return False
        return True
    
    return predicate


def _build_line_predicate(line: Optional[str]) -> Optional[Callable[[dict], bool]]:
    if not line:
        return None
    
    def predicate(message: dict) -> bool:
        return message.get("line") == line
    
    return predicate


async def _stream_websocket(
    websocket: WebSocket,
    stream_name: str,
    predicate: Optional[Callable[[dict], bool]] = None,
):
    await websocket.accept()
    if not await require_readonly_ws_source(websocket):
        return
    queue = ws_stream.register_subscriber(stream_name, predicate)
    logger.info("ws_client_connected", stream=stream_name)
    try:
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        logger.info("ws_client_disconnected", stream=stream_name)
    except Exception as exc:
        logger.exception("ws_stream_error", stream=stream_name, error=str(exc))
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
    finally:
        ws_stream.unregister_subscriber(stream_name, queue)


@router.websocket("/ws/realtime")
async def websocket_realtime(
    websocket: WebSocket,
    line: Optional[str] = Query(None, description="按产线过滤，例如 L1"),
    tags: Optional[List[str]] = Query(None, description="按标签过滤，可多值"),
):
    if tags:
        tag_set = {tag.strip() for tag in tags if tag.strip()}
    else:
        tag_set = None
    predicate = _build_realtime_predicate(line, tag_set)
    await _stream_websocket(websocket, "realtime", predicate)


@router.websocket("/ws/ai")
async def websocket_ai(
    websocket: WebSocket,
    line: Optional[str] = Query(None, description="按产线过滤，例如 L1"),
):
    predicate = _build_line_predicate(line)
    await _stream_websocket(websocket, "ai", predicate)


@router.websocket("/ws/system")
async def websocket_system(
    websocket: WebSocket,
    line: Optional[str] = Query(None, description="按产线过滤，例如 L1"),
):
    predicate = _build_line_predicate(line)
    await _stream_websocket(websocket, "system", predicate)


@router.get("/system/health", dependencies=[Depends(require_readonly_http_source)])
async def system_health(
    line: Optional[str] = Query(None, description="可选：按产线过滤"),
):
    data = ws_stream.get_latest("system", line)
    if not data:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No system health data available yet",
        )
    return {
        "success": True,
        "data": data,
        "timestamp": int(time.time()),
    }
