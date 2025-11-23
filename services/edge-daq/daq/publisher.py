"""
数据发布器 - MQTT + InfluxDB
"""

import json
import structlog
import paho.mqtt.client as mqtt
import os
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import ASYNCHRONOUS

logger = structlog.get_logger(__name__)


class DataPublisher:
    """数据发布器"""
    
    def __init__(self):
        self.mqtt_client = None
        self.influx_client = None
        self.write_api = None
        self.connected = False
        
        # 从环境变量读取配置
        self.mqtt_broker = os.getenv("MQTT_BROKER", "localhost")
        self.mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
        self.mqtt_user = os.getenv("MQTT_USER", "")
        self.mqtt_password = os.getenv("MQTT_PASSWORD", "")
        
        self.influx_host = os.getenv("INFLUXDB_HOST", "localhost")
        self.influx_port = os.getenv("INFLUXDB_PORT", "8086")
        self.influx_token = os.getenv("INFLUXDB_TOKEN", "")
        self.influx_org = os.getenv("INFLUXDB_ORG", "edge-org")
        self.influx_bucket = os.getenv("INFLUXDB_BUCKET", "industrial-data")
    
    async def connect(self):
        """连接到 MQTT 和 InfluxDB"""
        # 连接 MQTT
        await self._connect_mqtt()
        
        # 连接 InfluxDB
        await self._connect_influxdb()
        
        self.connected = True
        logger.info("data_publisher_connected")
    
    async def disconnect(self):
        """断开连接"""
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
        
        if self.influx_client:
            self.influx_client.close()
        
        self.connected = False
        logger.info("data_publisher_disconnected")
    
    async def _connect_mqtt(self):
        """连接到 MQTT Broker"""
        try:
            self.mqtt_client = mqtt.Client(client_id="edge-daq")
            
            if self.mqtt_user and self.mqtt_password:
                self.mqtt_client.username_pw_set(
                    self.mqtt_user,
                    self.mqtt_password,
                )
            
            self.mqtt_client.on_connect = self._on_mqtt_connect
            self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
            
            self.mqtt_client.connect(
                self.mqtt_broker,
                self.mqtt_port,
                keepalive=60,
            )
            
            self.mqtt_client.loop_start()
            
            logger.info(
                "mqtt_publisher_connected",
                broker=self.mqtt_broker,
                port=self.mqtt_port,
            )
        
        except Exception as e:
            logger.error("mqtt_connect_error", error=str(e))
    
    async def _connect_influxdb(self):
        """连接到 InfluxDB"""
        try:
            url = f"http://{self.influx_host}:{self.influx_port}"
            
            self.influx_client = InfluxDBClient(
                url=url,
                token=self.influx_token,
                org=self.influx_org,
            )
            
            self.write_api = self.influx_client.write_api(write_options=ASYNCHRONOUS)
            
            logger.info(
                "influxdb_publisher_connected",
                url=url,
                org=self.influx_org,
                bucket=self.influx_bucket,
            )
        
        except Exception as e:
            logger.error("influxdb_connect_error", error=str(e))
    
    def _on_mqtt_connect(self, client, userdata, flags, rc):
        """MQTT 连接回调"""
        if rc == 0:
            logger.info("mqtt_publisher_connected_callback")
        else:
            logger.error("mqtt_connection_failed", return_code=rc)
    
    def _on_mqtt_disconnect(self, client, userdata, rc):
        """MQTT 断开回调"""
        logger.warning("mqtt_publisher_disconnected", return_code=rc)
    
    async def publish_tag_data(self, tag_name: str, value: float, unit: str = "", timestamp: float = None):
        """
        发布标签数据
        
        Args:
            tag_name: 标签名称
            value: 数据值
            unit: 单位
            timestamp: 时间戳
        """
        import time
        if timestamp is None:
            timestamp = time.time()
        
        # 构造数据
        data = {
            "tag": tag_name,
            "value": value,
            "unit": unit,
            "timestamp": timestamp,
        }
        
        # 发布到 MQTT
        topic = f"industrial/dryer/realtime/{tag_name}"
        self._publish_mqtt(topic, data)
        
        # 写入 InfluxDB
        self._write_influxdb(tag_name, value, unit, timestamp)
    
    def _publish_mqtt(self, topic: str, data: dict, qos: int = 1):
        """发布 MQTT 消息"""
        if not self.mqtt_client:
            return
        
        try:
            payload = json.dumps(data)
            result = self.mqtt_client.publish(topic, payload, qos)
            
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                logger.error(
                    "mqtt_publish_failed",
                    topic=topic,
                    rc=result.rc,
                )
        
        except Exception as e:
            logger.error(
                "mqtt_publish_error",
                topic=topic,
                error=str(e),
            )
    
    def _write_influxdb(self, tag_name: str, value: float, unit: str, timestamp: float):
        """写入 InfluxDB"""
        if not self.write_api:
            return
        
        try:
            point = Point("industrial_data") \
                .tag("tag", tag_name) \
                .field("value", value) \
                .field("unit", unit) \
                .time(int(timestamp * 1e9))
            
            self.write_api.write(
                bucket=self.influx_bucket,
                org=self.influx_org,
                record=point,
            )
        
        except Exception as e:
            logger.error(
                "influxdb_write_error",
                tag=tag_name,
                error=str(e),
            )
    
    async def publish_alarm(self, tag_name: str, alarm_type: str, value: float, threshold: float):
        """
        发布报警消息
        
        Args:
            tag_name: 标签名称
            alarm_type: 报警类型
            value: 当前值
            threshold: 阈值
        """
        import time
        
        alarm_data = {
            "tag": tag_name,
            "type": alarm_type,
            "value": value,
            "threshold": threshold,
            "timestamp": time.time(),
        }
        
        topic = f"industrial/dryer/alarm/{tag_name}"
        self._publish_mqtt(topic, alarm_data, qos=2)
        
        logger.warning(
            "alarm_published",
            tag=tag_name,
            type=alarm_type,
            value=value,
            threshold=threshold,
        )

