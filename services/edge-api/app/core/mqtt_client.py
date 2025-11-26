"""
MQTT 客户端管理
"""

import paho.mqtt.client as mqtt
import structlog
import json
from typing import Callable, Optional
from app.core.config import settings

logger = structlog.get_logger(__name__)


class MQTTManager:
    """MQTT 客户端管理器"""
    
    def __init__(self):
        self.client: Optional[mqtt.Client] = None
        self.connected = False
        self.message_callbacks = {}
    
    async def connect(self):
        """连接到 MQTT Broker"""
        try:
            self.client = mqtt.Client(
                client_id=settings.MQTT_CLIENT_ID,
                protocol=mqtt.MQTTv311,
            )
            
            # 设置回调函数
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message
            
            # 设置认证
            self.client.username_pw_set(
                settings.MQTT_USER,
                settings.MQTT_PASSWORD,
            )
            
            # 连接到 broker
            self.client.connect(
                settings.MQTT_BROKER,
                settings.MQTT_PORT,
                keepalive=60,
            )
            
            # 启动网络循环
            self.client.loop_start()
            
            logger.info(
                "mqtt_connecting",
                broker=settings.MQTT_BROKER,
                port=settings.MQTT_PORT,
            )
        
        except Exception as e:
            logger.error("mqtt_connect_failed", error=str(e))
            raise
    
    async def disconnect(self):
        """断开 MQTT 连接"""
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            logger.info("mqtt_disconnected")
    
    def _on_connect(self, client, userdata, flags, rc):
        """连接成功回调"""
        if rc == 0:
            self.connected = True
            logger.info("mqtt_connected")
            
            # 订阅默认主题
            self.subscribe("realtime/#")
            self.subscribe("ai/#")
            self.subscribe("system/#")
        else:
            logger.error("mqtt_connection_failed", return_code=rc)
    
    def _on_disconnect(self, client, userdata, rc):
        """断开连接回调"""
        self.connected = False
        logger.warning("mqtt_disconnected", return_code=rc)
    
    def _on_message(self, client, userdata, msg):
        """收到消息回调"""
        try:
            topic = msg.topic
            payload = msg.payload.decode("utf-8")
            
            logger.debug(
                "mqtt_message_received",
                topic=topic,
                payload=payload,
            )
            
            # 调用注册的回调函数
            for pattern, callback in self.message_callbacks.items():
                if self._topic_matches(pattern, topic):
                    callback(topic, payload)
        
        except Exception as e:
            logger.error(
                "mqtt_message_processing_failed",
                topic=msg.topic,
                error=str(e),
            )
    
    def subscribe(self, topic: str, qos: int = 1):
        """订阅主题"""
        if self.client:
            self.client.subscribe(topic, qos)
            logger.info("mqtt_subscribed", topic=topic, qos=qos)
    
    def publish(self, topic: str, payload: dict, qos: int = 1):
        """发布消息"""
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
        """注册消息回调函数"""
        self.message_callbacks[topic_pattern] = callback
        logger.info("mqtt_callback_registered", pattern=topic_pattern)
    
    @staticmethod
    def _topic_matches(pattern: str, topic: str) -> bool:
        """检查主题是否匹配模式"""
        pattern_parts = pattern.split("/")
        topic_parts = topic.split("/")
        
        if len(pattern_parts) != len(topic_parts):
            # 处理 # 通配符
            if pattern_parts[-1] == "#":
                pattern_parts = pattern_parts[:-1]
                if len(pattern_parts) > len(topic_parts):
                    return False
            else:
                return False
        
        for p, t in zip(pattern_parts, topic_parts):
            if p != "+" and p != "#" and p != t:
                return False
        
        return True


# 创建全局 MQTT 管理器实例
mqtt_manager = MQTTManager()

