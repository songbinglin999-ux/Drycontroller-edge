"""
轻量事件总线：基于 mosquitto + asyncio.Queue

本模块实现了一个轻量级的事件总线，用于在系统内部分发 MQTT 消息。
支持主题订阅、消息发布和回调注册等核心功能。

主要特性：
- 统一封装 MQTT 订阅与本地事件推送
- 支持通配符主题匹配（# 和 + 通配符）
- 使用 asyncio.Queue 实现异步消息分发
- 提供统计信息用于监控和调试
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Protocol

import structlog

from app.core.mqtt_client import mqtt_manager


logger = structlog.get_logger(__name__)


class MQTTAdapterProtocol(Protocol):
    """MQTT 适配器协议定义，用于解耦 MQTT 客户端实现"""
    
    def subscribe(self, topic: str, qos: int = 1) -> None:
        """订阅 MQTT 主题"""
        ...

    def register_callback(
        self,
        topic_pattern: str,
        callback: Callable[[str, str], None],
    ) -> Callable[[str, str], None]:
        """注册消息回调函数"""
        ...

    def unregister_callback(
        self,
        topic_pattern: str,
        callback: Callable[[str, str], None],
    ) -> None:
        """注销消息回调函数"""
        ...


class EventBus:
    """
    统一事件总线：封装 publish/subscribe，支持 MQTT 与本地推送
    
    功能说明：
    - 订阅 MQTT 主题并转发到本地队列
    - 支持本地服务直接发布事件
    - 提供发布/订阅统计信息
    
    使用示例：
        # 订阅主题
        queue = event_bus.subscribe("realtime/#")
        
        # 消费消息
        event = await queue.get()
        
        # 发布本地事件
        event_bus.publish("system/L1/status", {"status": "healthy"})
    """

    def __init__(
        self,
        mqtt_adapter: MQTTAdapterProtocol = mqtt_manager,
        default_patterns: Optional[List[str]] = None,
    ) -> None:
        """
        初始化事件总线
        
        Args:
            mqtt_adapter: MQTT 客户端适配器
            default_patterns: 默认订阅的主题模式列表
        """
        self._mqtt = mqtt_adapter
        # 默认订阅实时数据和系统状态两类主题
        self._default_patterns = default_patterns or ["realtime/#", "system/#"]
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # 存储各主题模式对应的 MQTT 回调函数
        self._mqtt_callbacks: Dict[str, Callable[[str, str], None]] = {}
        # 存储各主题模式对应的订阅队列列表
        self._queues: Dict[str, List[asyncio.Queue]] = defaultdict(list)
        self._started = False
        # 事件统计计数器
        self._stats = {
            "published": 0,      # 本地发布数
            "mqtt_received": 0,  # MQTT 接收数
            "dropped": 0,        # 丢弃数（队列溢出）
        }
        self._published = 0
        self._dispatched = 0
        # 按主题统计消息数量
        self._topic_counts: Dict[str, int] = defaultdict(int)

    async def start(self):
        """
        启动事件总线
        
        订阅默认主题模式并开始接收消息
        """
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        # 订阅所有默认主题模式
        for pattern in self._default_patterns:
            self._ensure_mqtt_subscription(pattern)
        self._started = True
        logger.info("event_bus_started", patterns=list(self._default_patterns))

    async def stop(self):
        """
        停止事件总线
        
        注销所有 MQTT 回调并清理资源
        """
        for pattern, callback in list(self._mqtt_callbacks.items()):
            self._mqtt.unregister_callback(pattern, callback)
        self._mqtt_callbacks.clear()
        self._queues.clear()
        self._started = False
        self._loop = None
        logger.info("event_bus_stopped")

    def subscribe(self, topic_pattern: str, queue_size: int = 200) -> asyncio.Queue:
        """
        订阅指定主题模式
        
        Args:
            topic_pattern: 主题模式（支持 # 和 + 通配符）
            queue_size: 消息队列大小，超出时会丢弃旧消息
            
        Returns:
            用于接收消息的异步队列
        """
        self._ensure_started()
        queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self._queues[topic_pattern].append(queue)
        self._ensure_mqtt_subscription(topic_pattern)
        logger.info("event_bus_subscribed", pattern=topic_pattern)
        return queue

    def unsubscribe(self, topic_pattern: str, queue: asyncio.Queue):
        """
        取消订阅指定主题模式
        
        Args:
            topic_pattern: 主题模式
            queue: 之前订阅时返回的队列
        """
        queues = self._queues.get(topic_pattern)
        if not queues:
            return
        try:
            queues.remove(queue)
        except ValueError:
            return
        # 如果没有订阅者了，清理主题模式
        if not queues:
            self._queues.pop(topic_pattern, None)
        logger.info("event_bus_unsubscribed", pattern=topic_pattern)

    def publish(self, topic: str, payload):
        """
        发布本地事件
        
        供本地服务直接发布事件到事件总线，无需经过 MQTT。
        
        Args:
            topic: 事件主题
            payload: 事件数据（支持字典、字符串、字节等格式）
        """
        self._ensure_started()
        event = {"topic": topic, "data": self._decode_payload(payload)}
        self._stats["published"] += 1
        loop = self._loop
        if loop:
            loop.call_soon_threadsafe(self._dispatch, event)
        self._published += 1

    def _ensure_started(self):
        """检查事件总线是否已启动"""
        if not self._started or self._loop is None:
            raise RuntimeError("EventBus has not been started")

    def _ensure_mqtt_subscription(self, topic_pattern: str):
        """
        确保已订阅指定的 MQTT 主题模式
        
        如果尚未订阅，则创建回调处理器并注册
        """
        if topic_pattern in self._mqtt_callbacks:
            return

        def handler(topic: str, payload: str):
            """MQTT 消息处理器"""
            self._handle_mqtt_message(topic, payload)

        self._mqtt_callbacks[topic_pattern] = handler
        self._mqtt.subscribe(topic_pattern)
        self._mqtt.register_callback(topic_pattern, handler)

    def _handle_mqtt_message(self, topic: str, payload: str):
        """
        处理 MQTT 消息
        
        将 MQTT 消息转换为事件并分发到订阅队列
        """
        if not self._loop:
            return
        event = {"topic": topic, "data": self._decode_payload(payload)}
        self._stats["mqtt_received"] += 1
        loop = self._loop
        if loop:
            loop.call_soon_threadsafe(self._dispatch, event)
        self._published += 1

    def _dispatch(self, event: dict):
        """
        分发事件到匹配的订阅队列
        
        遍历所有订阅模式，将事件推送到匹配的队列
        """
        topic = event.get("topic", "")
        for pattern, queues in self._queues.items():
            if self._topic_matches(pattern, topic):
                for queue in list(queues):
                    self._safe_put(queue, event)
        self._dispatched += 1
        self._topic_counts[topic] += 1

    def _safe_put(self, queue: asyncio.Queue, data: dict):
        """
        安全地向队列添加数据
        
        如果队列已满，丢弃最旧的消息后重试（避免阻塞）
        """
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            # 队列溢出时丢弃旧消息
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            finally:
                try:
                    queue.put_nowait(data)
                except asyncio.QueueFull:
                    logger.warning("event_bus_queue_overflow")
                    self._stats["dropped"] += 1

    def get_stats(self) -> dict:
        """
        获取事件总线统计数据
        
        Returns:
            包含发布数、分发数、丢弃数、主题统计等信息的字典
        """
        return {
            "started": self._started,
            "counters": dict(self._stats),
            "published": self._published,
            "dispatched": self._dispatched,
            "topics": dict(self._topic_counts),
            "subscriptions": list(self._queues.keys()),
        }

    @staticmethod
    def _decode_payload(payload):
        """
        解码消息载荷
        
        统一将各种格式的 payload 转换为字典
        
        支持的输入格式：
        - None: 返回空字典
        - bytes/bytearray: 先解码为 UTF-8 字符串
        - str: 尝试 JSON 解析，失败则包装为 {"value": ...}
        - dict: 直接返回
        - 其他: 包装为 {"value": ...}
        """
        if payload is None:
            return {}
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8")
        if isinstance(payload, str):
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return {"value": payload}
        if isinstance(payload, dict):
            return payload
        return {"value": payload}

    @staticmethod
    def _topic_matches(pattern: str, topic: str) -> bool:
        """
        检查主题是否匹配模式
        
        MQTT 通配符规则：
        - #: 匹配多级主题（只能在末尾）
        - +: 匹配单级主题
        
        Args:
            pattern: 主题模式
            topic: 实际主题
            
        Returns:
            是否匹配
        """
        if pattern == topic:
            return True
        pattern_parts = pattern.split("/")
        topic_parts = topic.split("/")

        for idx, part in enumerate(pattern_parts):
            if part == "#":
                return True
            if idx >= len(topic_parts):
                return False
            if part != "+" and part != topic_parts[idx]:
                return False
        return len(pattern_parts) == len(topic_parts)


# 全局事件总线单例
event_bus = EventBus()
