"""实时数据 WebSocket API 和系统流兜底。"""

import time
from typing import Callable, List, Optional, Set

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi import WebSocket, WebSocketDisconnect

from app.services.ws_stream import ws_stream
from app.core.security import require_readonly_http_source, require_readonly_ws_source

router = APIRouter()
logger = structlog.get_logger(__name__)


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
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
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
