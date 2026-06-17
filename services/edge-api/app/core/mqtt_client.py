"""
MQTT 客户端管理

本模块封装了 MQTT 客户端的连接管理和消息处理功能。
作为边缘 API 服务与消息队列（Mosquitto）之间的通信桥梁。

主要功能：
- 连接管理：自动连接、断线重连
- 主题订阅：支持通配符模式（# 和 +）
- 消息发布：将数据推送到指定主题
- 回调注册：支持按主题模式注册多个消息处理器

使用方式：
    # 连接到 MQTT Broker
    await mqtt_manager.connect()
    
    # 注册消息回调
    mqtt_manager.register_callback("realtime/#", handle_realtime)
    
    # 发布消息
    mqtt_manager.publish("ai/moisture", {"value": 12.5})
"""

import json
from collections import defaultdict
from typing import Callable, Dict, List, Optional

import paho.mqtt.client as mqtt
import structlog
import asyncio
import os
import time

from app.core.config import settings

logger = structlog.get_logger(__name__)


class MQTTManager:
    """
    MQTT 客户端管理器
    
    负责与 MQTT Broker（Mosquitto）的连接管理，
    提供消息订阅、发布和回调分发功能。
    
    Attributes:
        client: paho-mqtt 客户端实例
        connected: 连接状态标志
        message_callbacks: 按主题模式注册的回调函数字典
    """
    
    def __init__(self):
        """初始化 MQTT 管理器"""
        self.client: Optional[mqtt.Client] = None
        self.connected = False
        # 存储各主题模式对应的回调函数列表
        self.message_callbacks: Dict[str, List[Callable]] = defaultdict(list)
    
    async def connect(self):
        """
        连接到 MQTT Broker
        
        使用配置文件中的 Broker 地址和认证信息建立连接。
        连接成功后自动订阅默认主题。
        """
        try:
            self.client = mqtt.Client(
                client_id=settings.MQTT_CLIENT_ID,
                protocol=mqtt.MQTTv311,  # 使用 MQTT 3.1.1 协议
            )
            
            # 设置事件回调函数
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message
            
            # 设置用户名密码认证
            self.client.username_pw_set(
                settings.MQTT_USER,
                settings.MQTT_PASSWORD,
            )

            # 自动重连退避：避免 broker 抖动导致连接风暴
            try:
                min_delay = int(os.getenv("MQTT_RECONNECT_MIN_DELAY", "1") or "1")
                max_delay = int(os.getenv("MQTT_RECONNECT_MAX_DELAY", "30") or "30")
                if hasattr(self.client, "reconnect_delay_set"):
                    self.client.reconnect_delay_set(min_delay=min_delay, max_delay=max_delay)
            except (TypeError, ValueError):
                # 不影响主流程
                pass

            # 异步连接 + 后台网络线程，断线后由 paho 自动重连（配合 reconnect_delay_set）
            self.client.connect_async(
                settings.MQTT_BROKER,
                settings.MQTT_PORT,
                keepalive=60,
            )

            # 启动后台网络循环线程
            self.client.loop_start()
            
            logger.info(
                "mqtt_connecting",
                broker=settings.MQTT_BROKER,
                port=settings.MQTT_PORT,
            )

            # 尝试等待一小段时间让首次连接完成（不阻塞过久，避免启动卡死）
            try:
                timeout = float(os.getenv("MQTT_CONNECT_TIMEOUT_SEC", "5") or "5")
            except (TypeError, ValueError):
                timeout = 5.0
            start = time.time()
            while not self.connected and time.time() - start < timeout:
                await asyncio.sleep(0.05)
            if not self.connected:
                logger.warning("mqtt_connect_timeout", timeout_sec=timeout)
        
        except (OSError, RuntimeError, ValueError) as e:
            logger.error("mqtt_connect_failed", error=str(e))
            raise
    
    async def disconnect(self):
        """
        断开 MQTT 连接
        
        停止后台网络循环并断开与 Broker 的连接
        """
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            logger.info("mqtt_disconnected")
    
    def _on_connect(self, client, userdata, flags, rc):
        """
        连接成功回调
        
        Args:
            client: MQTT 客户端实例
            userdata: 用户数据（未使用）
            flags: 连接标志
            rc: 返回码（0 表示成功）
        """
        if rc == 0:
            self.connected = True
            logger.info("mqtt_connected")
            
            # 自动订阅三个核心主题
            self.subscribe("realtime/#")  # 实时数据
            self.subscribe("ai/#")         # AI 推理结果
            self.subscribe("system/#")     # 系统状态
        else:
            logger.error("mqtt_connection_failed", return_code=rc)
    
    def _on_disconnect(self, client, userdata, rc):
        """
        断开连接回调
        
        Args:
            rc: 返回码（0 表示正常断开，非 0 表示异常断开）
        """
        self.connected = False
        logger.warning("mqtt_disconnected", return_code=rc)
    
    def _on_message(self, client, userdata, msg):
        """
        收到消息回调
        
        将消息分发给所有匹配主题模式的回调函数
        """
        try:
            topic = msg.topic
            payload = msg.payload.decode("utf-8")
            
            logger.debug(
                "mqtt_message_received",
                topic=topic,
                payload=payload,
            )
            
            # 遍历所有注册的回调，匹配主题模式
            for pattern, callbacks in list(self.message_callbacks.items()):
                if self._topic_matches(pattern, topic):
                    for callback in list(callbacks):
                        try:
                            callback(topic, payload)
                        except Exception:  # noqa: BLE001 - 故意宽捕：隔离单个 subscriber callback，不能影响 MQTT 网络线程/后续回调
                            logger.exception(
                                "mqtt_callback_failed",
                                pattern=pattern,
                                topic=topic,
                            )
        
        except Exception as e:  # noqa: BLE001 - 故意宽捕：Paho on_message 顶层边界，坏消息/回调异常只能记录不能杀网络线程
            logger.error(
                "mqtt_message_processing_failed",
                topic=msg.topic,
                error=str(e),
            )
    
    def subscribe(self, topic: str, qos: int = 1):
        """
        订阅 MQTT 主题
        
        Args:
            topic: 主题名称（支持通配符 # 和 +）
            qos: 服务质量等级（0/1/2），默认 1
        """
        if self.client:
            self.client.subscribe(topic, qos)
            logger.info("mqtt_subscribed", topic=topic, qos=qos)
    
    def publish(self, topic: str, payload: dict, qos: int = 1):
        """
        发布消息到指定主题
        
        Args:
            topic: 目标主题
            payload: 消息内容（字典，会被序列化为 JSON）
            qos: 服务质量等级
        """
        if self.client and self.connected:
            message = json.dumps(payload)
            result = self.client.publish(topic, message, qos)
            
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.debug("mqtt_message_published", topic=topic)
            else:
                logger.error("mqtt_publish_failed", topic=topic, rc=result.rc)
        else:
            logger.warning("mqtt_not_connected")
    
    def register_callback(self, topic_pattern: str, callback: Callable):
        """
        注册消息回调函数
        
        Args:
            topic_pattern: 主题模式（支持通配符）
            callback: 回调函数，签名为 (topic: str, payload: str) -> None
            
        Returns:
            注册的回调函数（方便链式调用）
        """
        self.message_callbacks[topic_pattern].append(callback)
        logger.info("mqtt_callback_registered", pattern=topic_pattern)
        return callback
    
    def unregister_callback(self, topic_pattern: str, callback: Callable):
        """
        注销消息回调函数
        
        Args:
            topic_pattern: 主题模式
            callback: 要注销的回调函数
        """
        callbacks = self.message_callbacks.get(topic_pattern)
        if not callbacks:
            return
        try:
            callbacks.remove(callback)
            logger.info("mqtt_callback_unregistered", pattern=topic_pattern)
        except ValueError:
            return
        # 如果该模式没有回调了，清理字典条目
        if not callbacks:
            self.message_callbacks.pop(topic_pattern, None)
    
    @staticmethod
    def _topic_matches(pattern: str, topic: str) -> bool:
        """
        检查主题是否匹配模式
        
        MQTT 通配符规则：
        - # 匹配多级主题（只能在末尾）
        - + 匹配单级主题
        
        Args:
            pattern: 主题模式（如 realtime/#、sensor/+/temp）
            topic: 实际主题（如 realtime/L1/tags）
            
        Returns:
            是否匹配
        """
        pattern_parts = pattern.split("/")
        topic_parts = topic.split("/")
        
        if len(pattern_parts) != len(topic_parts):
            # 处理 # 通配符（匹配剩余所有层级）
            if pattern_parts[-1] == "#":
                pattern_parts = pattern_parts[:-1]
                if len(pattern_parts) > len(topic_parts):
                    return False
            else:
                return False
        
        # 逐层比较
        for p, t in zip(pattern_parts, topic_parts):
            if p != "+" and p != "#" and p != t:
                return False
        
        return True


# 全局 MQTT 管理器单例
mqtt_manager = MQTTManager()
