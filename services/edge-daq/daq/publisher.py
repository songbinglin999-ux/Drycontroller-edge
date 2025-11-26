"""
数据发布器 - MQTT + InfluxDB

本模块负责将采集到的工业数据发布到两个目标：
1. MQTT 消息队列：用于实时数据推送到前端和其他服务
2. InfluxDB 时序数据库：用于历史数据存储和查询

数据流向：
┌─────────────┐    ┌─────────────┐
│   MQTT      │ <- │             │
│  (实时推送)  │    │ DataPublisher│
└─────────────┘    │             │
                   │             │
┌─────────────┐    │             │
│  InfluxDB   │ <- │             │
│  (历史存储)  │    └─────────────┘
└─────────────┘

环境变量配置：
- MQTT_BROKER/MQTT_PORT: MQTT 服务器地址
- INFLUXDB_HOST/INFLUXDB_PORT: InfluxDB 地址
- LINE_ID: 产线标识（用于消息路由）
"""

import json
import os
from typing import Optional

import paho.mqtt.client as mqtt
import structlog
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import ASYNCHRONOUS

logger = structlog.get_logger(__name__)


class DataPublisher:
    """
    数据发布器
    
    同时维护 MQTT 和 InfluxDB 两个连接，提供统一的数据发布接口。
    
    Attributes:
        mqtt_client: MQTT 客户端
        influx_client: InfluxDB 客户端
        write_api: InfluxDB 异步写入 API
        connected: 连接状态
        line_id: 产线标识，用于数据隔离
    """
    
    def __init__(self):
        """
        初始化发布器
        
        从环境变量读取所有连接配置
        """
        self.mqtt_client = None
        self.influx_client = None
        self.write_api = None
        self.connected = False
        
        # MQTT 配置
        self.mqtt_broker = os.getenv("MQTT_BROKER", "localhost")
        self.mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
        self.mqtt_user = os.getenv("MQTT_USER", "")
        self.mqtt_password = os.getenv("MQTT_PASSWORD", "")
        
        # 产线标识：用于区分不同生产线的数据
        self.line_id = os.getenv("LINE_ID", "L1")
        
        # InfluxDB 配置
        self.influx_host = os.getenv("INFLUXDB_HOST", "localhost")
        self.influx_port = os.getenv("INFLUXDB_PORT", "8086")
        self.influx_token = os.getenv("INFLUXDB_TOKEN", "")
        self.influx_org = os.getenv("INFLUXDB_ORG", "edge-org")
        self.influx_bucket = os.getenv("INFLUXDB_BUCKET", "industrial-data")
        self.measurement = os.getenv("INFLUXDB_MEASUREMENT", "industrial_data")

        # MQTT 主题配置
        self.realtime_topic = os.getenv(
            "MQTT_TOPIC_REALTIME",
            f"realtime/{self.line_id}/tags",  # 实时数据主题
        )
        self.alarm_topic = os.getenv(
            "MQTT_TOPIC_ALARM",
            f"system/{self.line_id}/alarms",  # 报警主题
        )
    
    async def connect(self):
        """
        建立所有连接
        
        同时连接 MQTT Broker 和 InfluxDB
        """
        # 连接 MQTT
        await self._connect_mqtt()
        
        # 连接 InfluxDB
        await self._connect_influxdb()
        
        self.connected = True
        logger.info("data_publisher_connected")
    
    async def disconnect(self):
        """
        断开所有连接
        
        停止 MQTT 网络循环并关闭 InfluxDB 客户端
        """
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
        
        if self.influx_client:
            self.influx_client.close()
        
        self.connected = False
        logger.info("data_publisher_disconnected")
    
    async def _connect_mqtt(self):
        """
        连接到 MQTT Broker
        
        配置认证、回调并启动后台网络循环
        """
        try:
            self.mqtt_client = mqtt.Client(client_id="edge-daq")
            
            # 配置用户名密码认证
            if self.mqtt_user and self.mqtt_password:
                self.mqtt_client.username_pw_set(
                    self.mqtt_user,
                    self.mqtt_password,
                )
            
            # 设置连接状态回调
            self.mqtt_client.on_connect = self._on_mqtt_connect
            self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
            
            # 连接到 Broker
            self.mqtt_client.connect(
                self.mqtt_broker,
                self.mqtt_port,
                keepalive=60,
            )
            
            # 启动后台线程处理网络通信
            self.mqtt_client.loop_start()
            
            logger.info(
                "mqtt_publisher_connected",
                broker=self.mqtt_broker,
                port=self.mqtt_port,
            )
        
        except Exception as e:
            logger.error("mqtt_connect_error", error=str(e))
    
    async def _connect_influxdb(self):
        """
        连接到 InfluxDB
        
        创建异步写入 API 以提高写入性能
        """
        try:
            url = f"http://{self.influx_host}:{self.influx_port}"
            
            self.influx_client = InfluxDBClient(
                url=url,
                token=self.influx_token,
                org=self.influx_org,
            )
            
            # 使用异步写入模式，提高吞吐量
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
        """MQTT 连接成功回调"""
        if rc == 0:
            logger.info("mqtt_publisher_connected_callback")
        else:
            logger.error("mqtt_connection_failed", return_code=rc)
    
    def _on_mqtt_disconnect(self, client, userdata, rc):
        """MQTT 断开连接回调"""
        logger.warning("mqtt_publisher_disconnected", return_code=rc)
    
    async def publish_tag_data(self, tag_name: str, value: float, unit: str = "", timestamp: Optional[float] = None):
        """
        发布标签数据
        
        同时将数据发送到 MQTT（实时推送）和 InfluxDB（历史存储）
        
        Args:
            tag_name: 标签名称（如 DRYER1.TEMP）
            value: 采集到的数值
            unit: 工程单位（如 ℃、%）
            timestamp: 时间戳（默认当前时间）
        """
        import time
        ts = time.time() if timestamp is None else timestamp
        
        # 构造消息数据
        data = {
            "line": self.line_id,   # 产线标识
            "tag": tag_name,         # 标签名
            "value": value,          # 数值
            "unit": unit,            # 单位
            "ts": int(ts),           # 时间戳（秒）
        }
        
        # 发布到 MQTT（供实时订阅者消费）
        topic = self.realtime_topic
        self._publish_mqtt(topic, data)
        
        # 写入 InfluxDB（供历史查询）
        self._write_influxdb(tag_name, value, unit, ts)
    
    def _publish_mqtt(self, topic: str, data: dict, qos: int = 1):
        """
        发布消息到 MQTT 主题
        
        Args:
            topic: 目标主题
            data: 消息数据（字典）
            qos: 服务质量等级（默认 1）
        """
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
        """
        写入数据到 InfluxDB
        
        使用 Line Protocol 格式写入时序数据点
        
        Args:
            tag_name: 标签名（作为 tag）
            value: 数值（作为 field）
            unit: 单位（作为 field）
            timestamp: 时间戳（纳秒精度）
        """
        if not self.write_api:
            return
        
        try:
            # 构建数据点
            point = Point(self.measurement) \
                .tag("tag", tag_name) \
                .field("value", value) \
                .field("unit", unit) \
                .time(int(timestamp * 1e9))  # 转换为纳秒
            
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
        
        将报警信息发送到专用的报警主题（QoS=2 确保送达）
        
        Args:
            tag_name: 触发报警的标签名
            alarm_type: 报警类型（"high" 或 "low"）
            value: 触发时的数值
            threshold: 报警阈值
        """
        import time
        
        alarm_data = {
            "line": self.line_id,
            "tag": tag_name,
            "type": alarm_type,
            "value": value,
            "threshold": threshold,
            "ts": int(time.time()),
        }
        
        # 使用 QoS=2 确保报警消息可靠送达
        topic = self.alarm_topic
        self._publish_mqtt(topic, alarm_data, qos=2)
        
        logger.warning(
            "alarm_published",
            tag=tag_name,
            type=alarm_type,
            value=value,
            threshold=threshold,
        )

