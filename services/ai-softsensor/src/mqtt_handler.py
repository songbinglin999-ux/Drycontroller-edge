"""
MQTT 消息处理器
"""

import json
import structlog
import paho.mqtt.client as mqtt
import os
import time

logger = structlog.get_logger(__name__)


class MQTTHandler:
    """MQTT 处理器"""
    
    def __init__(self, model, config):
        self.model = model
        self.config = config
        self.client = None
        self.data_cache = {}
        
        self.mqtt_broker = os.getenv("MQTT_BROKER", "localhost")
        self.mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
        self.mqtt_user = os.getenv("MQTT_USER", "")
        self.mqtt_password = os.getenv("MQTT_PASSWORD", "")
    
    async def connect(self):
        """连接 MQTT"""
        self.client = mqtt.Client(client_id="ai-softsensor")
        
        if self.mqtt_user and self.mqtt_password:
            self.client.username_pw_set(self.mqtt_user, self.mqtt_password)
        
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        
        self.client.connect(self.mqtt_broker, self.mqtt_port)
        self.client.loop_start()
        
        logger.info("mqtt_handler_connected")
    
    async def disconnect(self):
        """断开 MQTT"""
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
        
        logger.info("mqtt_handler_disconnected")
    
    def _on_connect(self, client, userdata, flags, rc):
        """连接回调"""
        if rc == 0:
            logger.info("mqtt_connected")
            # 订阅实时数据主题
            client.subscribe("industrial/dryer/realtime/#")
        else:
            logger.error("mqtt_connection_failed", rc=rc)
    
    def _on_message(self, client, userdata, msg):
        """消息回调"""
        try:
            payload = json.loads(msg.payload.decode())
            tag = payload.get("tag")
            value = payload.get("value")
            
            if tag and value is not None:
                self.data_cache[tag] = value
                
                # 检查是否收集到所有需要的特征
                if self._has_all_features():
                    self._run_inference()
        
        except Exception as e:
            logger.error("message_processing_error", error=str(e))
    
    def _has_all_features(self) -> bool:
        """检查是否收集到所有特征"""
        input_features = self.model.input_features
        
        for feature in input_features:
            if feature not in self.data_cache:
                return False
        
        return True
    
    def _run_inference(self):
        """运行推理"""
        result = self.model.inference(self.data_cache)
        
        if result:
            # 发布推理结果
            topic = "industrial/dryer/ai/moisture"
            payload = {
                **result,
                "timestamp": time.time(),
                "model_version": self.config.get("softsensor", {}).get("model_version", "v1.0.0"),
            }
            
            self.client.publish(topic, json.dumps(payload), qos=1)
            
            logger.info("inference_result_published", **result)

