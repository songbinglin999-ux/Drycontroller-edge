"""
实时数据 API
"""

import asyncio
import base64
import hashlib
import hmac
import json
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
from app.core.security import is_control_api_key_allowed
from app.core.config import settings

router = APIRouter()
logger = structlog.get_logger(__name__)
WS_AUTH_TIMEOUT_SEC = 5.0


def _server_identity_proofs(nonce: str) -> List[str]:
    keys = list(dict.fromkeys([*settings.ADMIN_API_KEYS, *settings.CONTROL_API_KEYS]))
    proofs = []
    for key in keys:
        digest = hmac.new(key.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).digest()
        proofs.append(base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="))
    return proofs


async def _require_ws_control_auth(websocket: WebSocket) -> bool:
    try:
        message = await asyncio.wait_for(websocket.receive_text(), timeout=WS_AUTH_TIMEOUT_SEC)
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return False

    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return False

    nonce = str(payload.get("nonce") or "")
    if payload.get("type") != "hello" or not nonce:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return False

    proofs = _server_identity_proofs(nonce)
    if not proofs:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return False

    await websocket.send_json(
        {
            "type": "server_hello",
            "nonce": nonce,
            "alg": "HMAC-SHA256",
            "proofs": proofs,
        }
    )

    try:
        message = await asyncio.wait_for(websocket.receive_text(), timeout=WS_AUTH_TIMEOUT_SEC)
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return False

    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return False

    if payload.get("type") != "auth" or not is_control_api_key_allowed(payload.get("token")):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return False

    await websocket.send_json({"type": "auth_ok"})
    return True


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
    service: RealtimeDataService = Depends(get_realtime_data_service),
):
    try:
        return await service.get_latest_payload(tags, lookback=lookback)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/history")
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


@router.websocket("/realtime/stream")
async def realtime_stream(websocket: WebSocket):
    await websocket.accept()
    if not await _require_ws_control_auth(websocket):
        return
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

    data_service = get_realtime_data_service()
    logger.info("realtime_stream_connected", tags=tags, interval=interval_seconds)

    try:
        while True:
            tag_list = data_service.resolve_tags(tags)
            payload = await data_service.get_latest_payload(tag_list, lookback=lookback)
            await websocket.send_json(
                {
                    "type": "realtime",
                    "tags": tag_list,
                    "data": payload["data"],
                    "missing": payload.get("missing", []),
                    "timestamp": payload["timestamp"],
                }
            )
            await asyncio.sleep(interval_seconds)
    except WebSocketDisconnect:
        logger.info("realtime_stream_disconnected")
    except Exception as exc:
        logger.exception("realtime_stream_error", error=str(exc))
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)


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


async def _stream_websocket(websocket: WebSocket, stream_name: str, predicate: Optional[Callable[[dict], bool]] = None):
    await websocket.accept()
    if not await _require_ws_control_auth(websocket):
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


@router.get("/system/health")
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
