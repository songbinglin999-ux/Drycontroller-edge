"""
MQTT 消息处理器
"""

import json
import structlog
import paho.mqtt.client as mqtt
import os
import time
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import ASYNCHRONOUS

logger = structlog.get_logger(__name__)


class MQTTHandler:
    """MQTT 处理器"""
    
    def __init__(self, model, config):
        self.model = model
        self.config = config
        self.client = None
        self.influx_client = None
        self.write_api = None
        self.data_cache = {}
        
        self.mqtt_broker = os.getenv("MQTT_BROKER", "localhost")
        self.mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
        self.mqtt_user = os.getenv("MQTT_USER", "")
        self.mqtt_password = os.getenv("MQTT_PASSWORD", "")
        self.line_id = os.getenv("LINE_ID", "L1")
        self.model_version = self.config.get("softsensor", {}).get("model_version", "v1.0.0")
        
        publish_cfg = self.config.get("softsensor", {}).get("publish", {})
        topic_template = publish_cfg.get("mqtt_topic", "ai/{line}/moisture")
        self.ai_topic = topic_template.format(line=self.line_id)
        self.realtime_topic = os.getenv(
            "MQTT_TOPIC_REALTIME",
            f"realtime/{self.line_id}/tags",
        )
        
        self.influx_host = os.getenv("INFLUXDB_HOST", "localhost")
        self.influx_port = os.getenv("INFLUXDB_PORT", "8086")
        self.influx_token = os.getenv("INFLUXDB_TOKEN", "")
        self.influx_org = os.getenv("INFLUXDB_ORG", "edge-org")
        self.influx_bucket = os.getenv("INFLUXDB_BUCKET", "industrial-data")
        self.measurement = os.getenv("INFLUXDB_MEASUREMENT", "industrial_data")
    
    async def connect(self):
        """连接 MQTT"""
        self.client = mqtt.Client(client_id="ai-softsensor")
        
        if self.mqtt_user and self.mqtt_password:
            self.client.username_pw_set(self.mqtt_user, self.mqtt_password)
        
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        
        self.client.connect(self.mqtt_broker, self.mqtt_port)
        self.client.loop_start()
        self._connect_influx()
        
        logger.info("mqtt_handler_connected")
    
    async def disconnect(self):
        """断开 MQTT"""
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
        
        if self.influx_client:
            self.influx_client.close()
        
        logger.info("mqtt_handler_disconnected")
    
    def _connect_influx(self):
        """初始化 InfluxDB 写入客户端"""
        try:
            url = f"http://{self.influx_host}:{self.influx_port}"
            self.influx_client = InfluxDBClient(
                url=url,
                token=self.influx_token,
                org=self.influx_org,
            )
            self.write_api = self.influx_client.write_api(write_options=ASYNCHRONOUS)
            logger.info("softsensor_influx_connected", url=url, bucket=self.influx_bucket)
        except Exception as exc:
            logger.error("softsensor_influx_connect_failed", error=str(exc))
    
    def _on_connect(self, client, userdata, flags, rc):
        """连接回调"""
        if rc == 0:
            logger.info("mqtt_connected")
            client.subscribe(self.realtime_topic)
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
            self._publish_result(result)
    
    def _publish_result(self, result: dict):
        """发布推理结果到 MQTT 与 InfluxDB"""
        timestamp = time.time()
        payload = {
            "line": self.line_id,
            "tag": self.model.output_tag,
            "value": result["predicted_moisture"],
            "unit": self.model.output_unit,
            "confidence": result["confidence"],
            "ts": int(timestamp),
            "model_version": self.model_version,
        }
        
        if self.client:
            self.client.publish(self.ai_topic, json.dumps(payload), qos=1)
        
        self._write_influx(payload, timestamp)
        
        logger.info(
            "inference_result_published",
            moisture=payload["value"],
            confidence=payload["confidence"],
            topic=self.ai_topic,
        )
    
    def _write_influx(self, payload: dict, timestamp: float):
        if not self.write_api:
            return
        
        try:
            point = (
                Point(self.measurement)
                .tag("tag", payload["tag"])
                .field("value", payload["value"])
                .field("unit", payload["unit"])
                .field("confidence", payload["confidence"])
                .time(int(timestamp * 1e9))
            )
            self.write_api.write(
                bucket=self.influx_bucket,
                org=self.influx_org,
                record=point,
            )
        except Exception as exc:
            logger.error("softsensor_influx_write_failed", error=str(exc))

