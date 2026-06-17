"""
MQTT → WebSocket 转发服务

本模块实现 MQTT 消息到 WebSocket 客户端的实时转发功能。
主要用于将后端采集的实时数据推送到前端大屏或监控界面。

主要功能：
- 订阅多个 MQTT 主题流（realtime、ai、system）
- 缓存各流的最新数据
- 支持 WebSocket 客户端订阅和过滤
- 消息广播到所有订阅者

架构说明：
- 消费 event_bus 中的消息
- 每个流（stream）维护独立的订阅者队列
- 支持按产线（line）过滤消息
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Callable, Dict, Optional

import structlog

from app.services.event_bus import event_bus

logger = structlog.get_logger(__name__)


class MQTTWebSocketRelay:
    """
    MQTT 到 WebSocket 的消息转发器
    
    工作流程：
    1. 启动时订阅 event_bus 中的多个主题流
    2. 后台任务持续消费队列消息
    3. 将消息广播给所有 WebSocket 订阅者
    4. 缓存各流的最新数据供查询
    
    Attributes:
        streams: 各数据流的配置和订阅者信息
        latest: 各流的最新数据缓存（按产线分组）
    """
    
    def __init__(self) -> None:
        """初始化转发器"""
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        # 存储各数据流的配置：{流名: {pattern, subscribers, queue}}
        self.streams: Dict[str, Dict] = {}
        # 缓存各流的最新数据：{流名: {产线ID: 数据}}
        self.latest: Dict[str, Dict[str, dict]] = defaultdict(dict)
        self.started = False
        # 后台消费任务列表
        self._bus_tasks: list[asyncio.Task] = []
    
    async def start(self):
        """
        启动转发服务
        
        注册三个核心数据流：
        - realtime: 实时采集数据
        - ai: AI 推理结果
        - system: 系统状态信息
        """
        if self.started:
            return
        self.loop = asyncio.get_running_loop()
        self._register_stream("realtime", "realtime/#")
        self._register_stream("ai", "ai/#")
        self._register_stream("system", "system/#")
        self.started = True
        logger.info("ws_stream_relay_started")
    
    async def stop(self):
        """
        停止转发服务
        
        取消所有后台任务并清理订阅
        """
        if not self.started:
            return
        # 取消所有后台消费任务
        for task in self._bus_tasks:
            task.cancel()
        await asyncio.gather(*self._bus_tasks, return_exceptions=True)
        self._bus_tasks.clear()
        # 取消 event_bus 订阅
        for stream in self.streams.values():
            queue = stream.get("queue")
            pattern = stream.get("pattern")
            if queue and pattern:
                event_bus.unsubscribe(pattern, queue)
        self.streams.clear()
        self.loop = None
        self.started = False
        logger.info("ws_stream_relay_stopped")
    
    def _register_stream(self, stream_name: str, topic_pattern: str):
        """
        注册一个数据流
        
        Args:
            stream_name: 流名称（如 realtime、ai、system）
            topic_pattern: MQTT 主题模式（如 realtime/#）
        """
        queue = event_bus.subscribe(topic_pattern)
        self.streams[stream_name] = {
            "pattern": topic_pattern,
            "subscribers": {},  # WebSocket 订阅者队列
            "queue": queue,     # 从 event_bus 接收消息的队列
        }
        # 启动后台任务消费消息
        task = asyncio.create_task(self._consume_bus(stream_name, queue))
        self._bus_tasks.append(task)

    async def _consume_bus(self, stream_name: str, queue: asyncio.Queue):
        """
        消费 event_bus 消息的后台任务
        
        持续从队列获取消息并处理，直到任务被取消
        """
        while True:
            try:
                event = await queue.get()
            except asyncio.CancelledError:
                break
            topic = event.get("topic", "")
            payload = event.get("data")
            try:
                self._handle_message(stream_name, topic, payload)
            except Exception:  # noqa: BLE001 - 故意宽捕：见下行 7x24 韧性边界
                # 7x24：避免单条坏消息/处理异常导致后台任务整体退出
                logger.exception("ws_stream_handle_message_failed", stream=stream_name, topic=topic)
    
    def _handle_message(self, stream_name: str, topic: str, payload):
        """
        处理收到的消息
        
        1. 解析 payload 为字典格式
        2. 更新最新数据缓存
        3. 广播给所有订阅者
        """
        if not self.loop or stream_name not in self.streams:
            return
        try:
            # 统一将 payload 转换为字典
            data: dict
            if isinstance(payload, dict):
                data = dict(payload)
            elif isinstance(payload, str):
                data = json.loads(payload)
            elif payload is None:
                data = {}
            else:
                data = {"value": payload}
        except json.JSONDecodeError:
            logger.warning("ws_stream_invalid_payload", topic=topic)
            return
        
        # 补充主题信息
        data.setdefault("topic", topic)
        
        # 更新缓存：按产线分组 + 全局最新
        line = data.get("line")
        if line:
            self.latest[stream_name][line] = data
        self.latest[stream_name]["__latest__"] = data
        
        # 广播给订阅者
        self.loop.call_soon_threadsafe(self._broadcast, stream_name, data)
    
    def _broadcast(self, stream_name: str, data: dict):
        """
        广播消息给指定流的所有订阅者
        
        支持订阅者设置过滤条件（predicate）
        """
        stream = self.streams.get(stream_name)
        if not stream:
            return
        subscribers: Dict[asyncio.Queue, Optional[Callable]] = stream["subscribers"]
        if not subscribers:
            return
        for queue, predicate in list(subscribers.items()):
            # 如果设置了过滤条件且不满足，跳过
            if predicate and not predicate(data):
                continue
            self._safe_put(queue, data)
    
    @staticmethod
    def _safe_put(queue: asyncio.Queue, data: dict):
        """
        安全地向队列添加数据
        
        如果队列已满，丢弃最旧的消息后重试
        """
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            # 队列满时，先取出一条旧消息再放入新消息
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
        """
        注册一个 WebSocket 订阅者
        
        Args:
            stream_name: 要订阅的流名称（realtime/ai/system）
            predicate: 可选的消息过滤函数，返回 True 表示接收
            queue_size: 消息队列大小，默认 200
            
        Returns:
            用于接收消息的异步队列
            
        Raises:
            ValueError: 流名称不存在
        """
        if stream_name not in self.streams:
            raise ValueError(f"Unknown stream '{stream_name}'")
        queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self.streams[stream_name]["subscribers"][queue] = predicate
        logger.info("ws_stream_subscribed", stream=stream_name, queue_id=id(queue))
        return queue
    
    def unregister_subscriber(self, stream_name: str, queue: asyncio.Queue):
        """
        注销 WebSocket 订阅者
        
        Args:
            stream_name: 流名称
            queue: 注册时返回的队列
        """
        stream = self.streams.get(stream_name)
        if not stream:
            return
        stream["subscribers"].pop(queue, None)
        logger.info("ws_stream_unsubscribed", stream=stream_name, queue_id=id(queue))
    
    def get_latest(self, stream_name: str, line: Optional[str] = None) -> Optional[dict]:
        """
        获取指定流的最新数据
        
        Args:
            stream_name: 流名称
            line: 可选的产线 ID，用于获取特定产线的数据
            
        Returns:
            最新的数据字典，如果没有数据返回 None
        """
        stream_latest = self.latest.get(stream_name)
        if not stream_latest:
            return None
        # 优先返回指定产线的数据
        if line and line in stream_latest:
            return stream_latest[line]
        # 否则返回全局最新
        return stream_latest.get("__latest__")


# 全局单例实例
ws_stream = MQTTWebSocketRelay()
