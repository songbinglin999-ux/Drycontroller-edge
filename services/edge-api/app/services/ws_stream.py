"""
MQTT → WebSocket 转发服务
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Callable, Dict, Optional

import structlog

from app.core.mqtt_client import mqtt_manager

logger = structlog.get_logger(__name__)


class MQTTWebSocketRelay:
    def __init__(self) -> None:
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.streams: Dict[str, Dict] = {}
        self.handlers: Dict[str, Callable] = {}
        self.latest: Dict[str, Dict[str, dict]] = defaultdict(dict)
        self.started = False
    
    async def start(self):
        if self.started:
            return
        self.loop = asyncio.get_running_loop()
        self._register_stream("realtime", "realtime/#")
        self._register_stream("ai", "ai/#")
        self._register_stream("system", "system/#")
        self.started = True
        logger.info("ws_stream_relay_started")
    
    async def stop(self):
        if not self.started:
            return
        for stream_name, handler in list(self.handlers.items()):
            pattern = self.streams[stream_name]["pattern"]
            mqtt_manager.unregister_callback(pattern, handler)
        self.streams.clear()
        self.handlers.clear()
        self.loop = None
        self.started = False
        logger.info("ws_stream_relay_stopped")
    
    def _register_stream(self, stream_name: str, topic_pattern: str):
        def handler(topic: str, payload: str):
            self._handle_message(stream_name, topic, payload)
        
        mqtt_manager.register_callback(topic_pattern, handler)
        self.streams[stream_name] = {
            "pattern": topic_pattern,
            "subscribers": {},
        }
        self.handlers[stream_name] = handler
    
    def _handle_message(self, stream_name: str, topic: str, payload: str):
        if not self.loop or stream_name not in self.streams:
            return
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("ws_stream_invalid_payload", topic=topic)
            return
        data.setdefault("topic", topic)
        line = data.get("line")
        if line:
            self.latest[stream_name][line] = data
        self.latest[stream_name]["__latest__"] = data
        self.loop.call_soon_threadsafe(self._broadcast, stream_name, data)
    
    def _broadcast(self, stream_name: str, data: dict):
        stream = self.streams.get(stream_name)
        if not stream:
            return
        subscribers: Dict[asyncio.Queue, Optional[Callable]] = stream["subscribers"]
        if not subscribers:
            return
        for queue, predicate in list(subscribers.items()):
            if predicate and not predicate(data):
                continue
            self._safe_put(queue, data)
    
    @staticmethod
    def _safe_put(queue: asyncio.Queue, data: dict):
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            finally:
                try:
                    queue.put_nowait(data)
                except asyncio.QueueFull:
                    logger.warning("ws_stream_drop_message")
    
    def register_subscriber(
        self,
        stream_name: str,
        predicate: Optional[Callable[[dict], bool]] = None,
        queue_size: int = 200,
    ) -> asyncio.Queue:
        if stream_name not in self.streams:
            raise ValueError(f"Unknown stream '{stream_name}'")
        queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self.streams[stream_name]["subscribers"][queue] = predicate
        logger.info("ws_stream_subscribed", stream=stream_name, queue_id=id(queue))
        return queue
    
    def unregister_subscriber(self, stream_name: str, queue: asyncio.Queue):
        stream = self.streams.get(stream_name)
        if not stream:
            return
        stream["subscribers"].pop(queue, None)
        logger.info("ws_stream_unsubscribed", stream=stream_name, queue_id=id(queue))
    
    def get_latest(self, stream_name: str, line: Optional[str] = None) -> Optional[dict]:
        stream_latest = self.latest.get(stream_name)
        if not stream_latest:
            return None
        if line and line in stream_latest:
            return stream_latest[line]
        return stream_latest.get("__latest__")


ws_stream = MQTTWebSocketRelay()

