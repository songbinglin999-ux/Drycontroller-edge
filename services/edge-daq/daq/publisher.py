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

import asyncio
import json
import os
import time
from typing import Optional
from collections import deque
from pathlib import Path

import paho.mqtt.client as mqtt
import structlog
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import ASYNCHRONOUS
from prometheus_client import Counter, Gauge

logger = structlog.get_logger(__name__)

MQTT_PUBLISH_FAILURE = Counter("mqtt_publish_failure_total", "MQTT publish failures", ["reason"])
MQTT_PUBLISH_RETRY = Counter("mqtt_publish_retry_total", "MQTT publish retry attempts")
MQTT_RECONNECT_TOTAL = Counter("mqtt_reconnect_total", "MQTT reconnect attempts")
MQTT_CONNECTED = Gauge("mqtt_connected", "MQTT connection state")

INFLUX_WRITE_FAILURE = Counter("influx_write_failure_total", "InfluxDB write failures", ["reason"])
INFLUX_WRITE_RETRY = Counter("influx_write_retry_total", "InfluxDB write retry attempts")
INFLUX_CONNECTED = Gauge("influx_connected", "InfluxDB connection state")
MQTT_BUFFER_SIZE = Gauge("mqtt_buffer_size", "MQTT pending buffer size")
INFLUX_BUFFER_SIZE = Gauge("influx_buffer_size", "Influx pending buffer size")
MQTT_BUFFER_OLDEST_AGE_SEC = Gauge("mqtt_buffer_oldest_age_sec", "Oldest MQTT buffered message age (s)")
INFLUX_BUFFER_OLDEST_AGE_SEC = Gauge("influx_buffer_oldest_age_sec", "Oldest Influx buffered point age (s)")
INFLUX_PENDING_BATCH_SIZE = Gauge("influx_pending_batch_size", "Pending batch points before write")
INFLUX_WRITE_RATE_LIMITED = Counter("influx_write_rate_limited_total", "Influx writes deferred by rate limit")
INFLUX_BUFFER_WARN = Counter("influx_buffer_warn_total", "Influx buffer exceeded warn threshold")
MQTT_BUFFER_WARN = Counter("mqtt_buffer_warn_total", "MQTT buffer exceeded warn threshold")


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
        self._mqtt_reconnect_backoff = 1.0
        self._mqtt_reconnect_backoff_max = 30.0
        self.mqtt_connected = False
        self.influx_connected = False
        self.last_mqtt_error: Optional[str] = None
        self.last_influx_error: Optional[str] = None
        self.mqtt_buffer = deque(maxlen=int(os.getenv("MQTT_BUFFER_MAX", "1000") or "1000"))
        self.influx_buffer = deque(maxlen=int(os.getenv("INFLUX_BUFFER_MAX", "2000") or "2000"))
        # MQTT 配置
        self.mqtt_broker = os.getenv("MQTT_BROKER", "localhost")
        self.mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
        self.mqtt_user = os.getenv("MQTT_USER", "")
        self.mqtt_password = os.getenv("MQTT_PASSWORD", "")
        self.mqtt_tls = os.getenv("MQTT_TLS", "false").lower() == "true"
        self.mqtt_tls_insecure = os.getenv("MQTT_TLS_INSECURE", "false").lower() == "true"
        self.mqtt_allow_insecure = os.getenv("MQTT_ALLOW_INSECURE", "false").lower() == "true"
        self.mqtt_require_auth = os.getenv("MQTT_REQUIRE_AUTH", "true").lower() == "true"
        
        # 产线标识：用于区分不同生产线的数据
        self.line_id = os.getenv("LINE_ID", "L1")
        
        # InfluxDB 配置
        self.influx_host = os.getenv("INFLUXDB_HOST", "localhost")
        self.influx_port = os.getenv("INFLUXDB_PORT", "8086")
        self.influx_scheme = os.getenv("INFLUXDB_SCHEME", "http")
        self.influx_token = os.getenv("INFLUXDB_TOKEN", "")
        self.influx_org = os.getenv("INFLUXDB_ORG", "edge-org")
        self.influx_bucket = os.getenv("INFLUXDB_BUCKET", "industrial-data")
        self.measurement = os.getenv("INFLUXDB_MEASUREMENT", "industrial_data")
        self.influx_require_auth = os.getenv("INFLUX_REQUIRE_AUTH", "true").lower() == "true"
        self.influx_host_backup = os.getenv("INFLUXDB_HOST_BACKUP", "")
        self.influx_port_backup = os.getenv("INFLUXDB_PORT_BACKUP", "")
        self.influx_scheme_backup = os.getenv("INFLUXDB_SCHEME_BACKUP", self.influx_scheme)
        self.influx_tls_ca = os.getenv("INFLUXDB_TLS_CA", "")
        self.influx_tls_cert = os.getenv("INFLUXDB_TLS_CERT", "")
        self.influx_tls_key = os.getenv("INFLUXDB_TLS_KEY", "")
        self.influx_verify_ssl = os.getenv("INFLUXDB_VERIFY_SSL", "true").lower() == "true"

        # MQTT 主题配置
        self.realtime_topic = os.getenv(
            "MQTT_TOPIC_REALTIME",
            f"realtime/{self.line_id}/tags",  # 实时数据主题
        )
        self.alarm_topic = os.getenv(
            "MQTT_TOPIC_ALARM",
            f"system/{self.line_id}/alarms",  # 报警主题
        )
        # 缓冲落盘配置
        self.buffer_dir = Path(os.getenv("BUFFER_DIR", "/data/edge-daq"))
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        self.mqtt_buffer_file = self.buffer_dir / "mqtt_buffer.jsonl"
        self.influx_buffer_file = self.buffer_dir / "influx_buffer.jsonl"
        self.persist_mqtt_buffer = os.getenv("MQTT_BUFFER_PERSIST", "true").lower() == "true"
        self.persist_influx_buffer = os.getenv("INFLUX_BUFFER_PERSIST", "true").lower() == "true"
        self._load_persisted_buffers()
        # 缓冲自动重放设置
        self.buffer_flush_interval = float(os.getenv("BUFFER_FLUSH_INTERVAL", "5") or "5")
        self._flush_task = None
        # Influx 批量与速率控制
        self.influx_batch_max = int(os.getenv("INFLUX_BATCH_MAX", "500") or "500")
        self.influx_flush_interval = float(os.getenv("INFLUX_FLUSH_INTERVAL", "2") or "2")
        self.influx_max_points_per_sec = int(os.getenv("INFLUX_MAX_POINTS_PER_SEC", "0") or "0")
        self.influx_pending_batch = []
        self._last_influx_flush_ts = time.time()
        self._rate_window_start = time.time()
        self._rate_points = 0
        # 时间戳质量控制
        self.ts_max_skew_sec = float(os.getenv("TS_MAX_SKEW_SEC", "300") or "300")
        # 缓冲告警阈值
        self.mqtt_buffer_warn = int(os.getenv("MQTT_BUFFER_WARN", "800") or "800")
        self.influx_buffer_warn = int(os.getenv("INFLUX_BUFFER_WARN", "1600") or "1600")
        self._buffer_warned_mqtt = False
        self._buffer_warned_influx = False
    
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
        # 启动后台缓冲冲刷，避免长时间堆积
        if self.buffer_flush_interval > 0:
            loop = asyncio.get_running_loop()
            self._flush_task = loop.create_task(self._flush_loop(), name="publisher_flush_loop")
        logger.info("data_publisher_connected")
    
    async def disconnect(self):
        """
        断开所有连接
        
        停止 MQTT 网络循环并关闭 InfluxDB 客户端
        """
        self.connected = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self.flush_buffers()
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            MQTT_CONNECTED.set(0)
            self.mqtt_connected = False
        
        if self.influx_client:
            self.influx_client.close()
            INFLUX_CONNECTED.set(0)
            self.influx_connected = False
        
        self.connected = False
        logger.info("data_publisher_disconnected")
    
    async def _flush_loop(self):
        """后台定时重放缓冲，连接恢复后自动补数据。"""
        try:
            while self.connected:
                await asyncio.sleep(self.buffer_flush_interval)
                self._flush_influx_pending_batch()
                await self.flush_buffers()
        except asyncio.CancelledError:
            return
    
    async def _connect_mqtt(self):
        """
        连接到 MQTT Broker
        
        配置认证、回调并启动后台网络循环
        """
        try:
            if not self.mqtt_tls and not self.mqtt_allow_insecure:
                raise RuntimeError("MQTT TLS is required. Set MQTT_TLS=true or explicitly allow insecure via MQTT_ALLOW_INSECURE=true.")
            if self.mqtt_require_auth and not (self.mqtt_user and self.mqtt_password):
                raise RuntimeError("MQTT auth required. Provide MQTT_USER/MQTT_PASSWORD or set MQTT_REQUIRE_AUTH=false explicitly.")
            self.mqtt_client = mqtt.Client(client_id="edge-daq")
            
            # 配置用户名密码认证
            if self.mqtt_user and self.mqtt_password:
                self.mqtt_client.username_pw_set(
                    self.mqtt_user,
                    self.mqtt_password,
                )
            
            # TLS
            if self.mqtt_tls:
                self.mqtt_client.tls_set()
                if self.mqtt_tls_insecure:
                    self.mqtt_client.tls_insecure_set(True)

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
            self._mqtt_reconnect_backoff = 1.0
            
            logger.info(
                "mqtt_publisher_connected",
                broker=self.mqtt_broker,
                port=self.mqtt_port,
            )
        
        except Exception as e:
            logger.error("mqtt_connect_error", error=str(e))
            self.connected = False
            self.last_mqtt_error = str(e)
    
    async def _connect_influxdb(self):
        """
        连接到 InfluxDB
        
        创建异步写入 API 以提高写入性能
        """
        try:
            def _connect_one(host, port, scheme):
                url = f"{scheme}://{host}:{port}"
                client = InfluxDBClient(
                    url=url,
                    token=self.influx_token,
                    org=self.influx_org,
                    ssl_ca_cert=self.influx_tls_ca or None,
                    ssl_cert_file=self.influx_tls_cert or None,
                    ssl_key_file=self.influx_tls_key or None,
                    verify_ssl=self.influx_verify_ssl,
                )
                return url, client

            if self.influx_require_auth and not self.influx_token:
                raise RuntimeError("InfluxDB token is required. Set INFLUXDB_TOKEN or INFLUX_REQUIRE_AUTH=false explicitly.")

            url = None
            client = None
            try:
                url, client = _connect_one(self.influx_host, self.influx_port, self.influx_scheme)
                client.health()
            except Exception as primary_err:
                if not self.influx_host_backup:
                    raise primary_err
                logger.warning("influx_primary_connect_failed_try_backup", error=str(primary_err))
                url, client = _connect_one(
                    self.influx_host_backup,
                    self.influx_port_backup or self.influx_port,
                    self.influx_scheme_backup,
                )
                client.health()

            self.influx_client = client
            self.write_api = self.influx_client.write_api(write_options=ASYNCHRONOUS)

            logger.info(
                "influxdb_publisher_connected",
                url=url,
                org=self.influx_org,
                bucket=self.influx_bucket,
            )
            INFLUX_CONNECTED.set(1)
            self.influx_connected = True

        except Exception as e:
            logger.error("influxdb_connect_error", error=str(e))
            self.connected = False
            self.last_influx_error = str(e)
            INFLUX_CONNECTED.set(0)
            self.influx_connected = False

    async def _ensure_mqtt(self):
        """
        确保 MQTT 客户端处于连接状态，失败时按指数退避重连。
        """
        if self.mqtt_client and getattr(self.mqtt_client, "is_connected", lambda: False)():
            return True
        backoff = self._mqtt_reconnect_backoff
        await self._connect_mqtt()
        # 逐步增大退避时间，避免连接风暴
        self._mqtt_reconnect_backoff = min(self._mqtt_reconnect_backoff * 2, self._mqtt_reconnect_backoff_max)
        if self.mqtt_client and getattr(self.mqtt_client, "is_connected", lambda: False)():
            return True
        # 等待退避时间后由调用方决定是否继续
        await asyncio.sleep(backoff)
        return False
    
    async def flush_buffers(self):
        """尽力刷新缓冲队列，避免停机丢数据。"""
        self._flush_influx_pending_batch()
        await self._flush_mqtt_buffer()
        await self._flush_influx_buffer()
    
    def _on_mqtt_connect(self, client, userdata, flags, rc):
        """MQTT 连接成功回调"""
        if rc == 0:
            logger.info("mqtt_publisher_connected_callback")
            self.mqtt_connected = True
            MQTT_CONNECTED.set(1)
            MQTT_RECONNECT_TOTAL.inc()
        else:
            logger.error("mqtt_connection_failed", return_code=rc)
            self.last_mqtt_error = f"rc={rc}"
            MQTT_CONNECTED.set(0)
            self.mqtt_connected = False
    
    def _on_mqtt_disconnect(self, client, userdata, rc):
        """MQTT 断开连接回调"""
        logger.warning("mqtt_publisher_disconnected", return_code=rc)
        MQTT_CONNECTED.set(0)
        self.mqtt_connected = False
    
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
    
    def _publish_mqtt(self, topic: str, data: dict, qos: int = 1, retries: int = 2):
        """
        发布消息到 MQTT 主题
        
        Args:
            topic: 目标主题
            data: 消息数据（字典）
            qos: 服务质量等级（默认 1）
        """
        if not self.mqtt_client:
            return
        
        attempt = 0
        while attempt <= retries:
            attempt += 1
            try:
                payload = json.dumps(data)
                result = self.mqtt_client.publish(topic, payload, qos)
                
                if result.rc == mqtt.MQTT_ERR_SUCCESS:
                    return

                reason = str(result.rc)
                MQTT_PUBLISH_FAILURE.labels(reason).inc()
                logger.error(
                    "mqtt_publish_failed",
                    topic=topic,
                    rc=result.rc,
                    attempt=attempt,
                )
            except Exception as e:
                reason = e.__class__.__name__
                MQTT_PUBLISH_FAILURE.labels(reason).inc()
                self.last_mqtt_error = str(e)
                logger.error(
                    "mqtt_publish_error",
                    topic=topic,
                    error=str(e),
                    attempt=attempt,
                )

            # 确保连接并退避后重试
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self._ensure_mqtt())
                else:
                    loop.run_until_complete(self._ensure_mqtt())
            except RuntimeError:
                # 没有事件循环时创建新的
                asyncio.run(self._ensure_mqtt())

            MQTT_PUBLISH_RETRY.inc()
            time.sleep(min(0.2 * (2 ** (attempt - 1)), 2.0))
        
        # 超过重试仍失败，写入缓冲
        self._buffer_mqtt(topic, data, qos)
    
    def _write_influxdb(self, tag_name: str, value: float, unit: str, timestamp: float, retries: int = 2):
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
            self._buffer_influx(tag_name, value, unit, timestamp)
            return

        now = time.time()
        if self.ts_max_skew_sec > 0 and abs(now - timestamp) > self.ts_max_skew_sec:
            logger.warning("ts_skew_detected", tag=tag_name, ts=timestamp, now=now, skew=now - timestamp)

        # 速率限制：超过速率则缓冲，避免打爆写入
        if self.influx_max_points_per_sec > 0:
            if now - self._rate_window_start >= 1:
                self._rate_window_start = now
                self._rate_points = 0
            if self._rate_points >= self.influx_max_points_per_sec:
                INFLUX_WRITE_RATE_LIMITED.inc()
                self._buffer_influx(tag_name, value, unit, timestamp)
                return
            self._rate_points += 1

        point = Point(self.measurement) \
            .tag("tag", tag_name) \
            .field("value", value) \
            .field("unit", unit) \
            .time(int(timestamp * 1e9))  # 转换为纳秒

        self.influx_pending_batch.append(point)
        INFLUX_PENDING_BATCH_SIZE.set(len(self.influx_pending_batch))

        if len(self.influx_pending_batch) >= self.influx_batch_max or (now - self._last_influx_flush_ts) >= self.influx_flush_interval:
            self._flush_influx_pending_batch()

    def _buffer_mqtt(self, topic: str, data: dict, qos: int):
        """将 MQTT 消息加入缓冲，超过上限会丢弃最旧并告警。"""
        if len(self.mqtt_buffer) >= self.mqtt_buffer.maxlen:
            logger.warning("mqtt_buffer_dropped_oldest", maxlen=self.mqtt_buffer.maxlen)
            self.mqtt_buffer.popleft()
        entry = {"topic": topic, "data": data, "qos": qos, "ts": int(time.time())}
        self.mqtt_buffer.append(entry)
        MQTT_BUFFER_SIZE.set(len(self.mqtt_buffer))
        self._persist_buffer(self.mqtt_buffer_file, entry, enable=self.persist_mqtt_buffer)
        self._update_buffer_age_metrics()
        self._check_buffer_thresholds()

    def _buffer_influx(self, tag_name: str, value: float, unit: str, timestamp: float):
        """将 Influx 数据加入缓冲。"""
        if len(self.influx_buffer) >= self.influx_buffer.maxlen:
            logger.warning("influx_buffer_dropped_oldest", maxlen=self.influx_buffer.maxlen)
            self.influx_buffer.popleft()
        entry = {"tag": tag_name, "value": value, "unit": unit, "ts": timestamp}
        self.influx_buffer.append(entry)
        INFLUX_BUFFER_SIZE.set(len(self.influx_buffer))
        self._persist_buffer(self.influx_buffer_file, entry, enable=self.persist_influx_buffer)
        self._update_buffer_age_metrics()
        self._check_buffer_thresholds()

    async def _flush_mqtt_buffer(self):
        """重放 MQTT 缓冲队列。"""
        if not self.mqtt_buffer:
            return
        if not (self.mqtt_client and getattr(self.mqtt_client, "is_connected", lambda: False)()):
            await self._ensure_mqtt()
        flushed = 0
        for _ in range(len(self.mqtt_buffer)):
            item = self.mqtt_buffer.popleft()
            MQTT_BUFFER_SIZE.set(len(self.mqtt_buffer))
            try:
                payload = json.dumps(item["data"])
                result = self.mqtt_client.publish(item["topic"], payload, item["qos"])
                if result.rc != mqtt.MQTT_ERR_SUCCESS:
                    # 失败则重新放回末尾
                    self.mqtt_buffer.append(item)
                    MQTT_BUFFER_SIZE.set(len(self.mqtt_buffer))
                    break
                flushed += 1
            except Exception as e:
                self.last_mqtt_error = str(e)
                self.mqtt_buffer.append(item)
                MQTT_BUFFER_SIZE.set(len(self.mqtt_buffer))
                break
        if flushed:
            logger.info("mqtt_buffer_flushed", count=flushed)
            self._persist_full_buffer(self.mqtt_buffer_file, list(self.mqtt_buffer), enable=self.persist_mqtt_buffer)
            self._update_buffer_age_metrics()

    async def _flush_influx_buffer(self):
        """重放 Influx 缓冲队列。"""
        if not self.influx_buffer or not self.write_api:
            return
        flushed = 0
        for _ in range(len(self.influx_buffer)):
            item = self.influx_buffer.popleft()
            INFLUX_BUFFER_SIZE.set(len(self.influx_buffer))
            try:
                point = Point(self.measurement) \
                    .tag("tag", item["tag"]) \
                    .field("value", item["value"]) \
                    .field("unit", item["unit"]) \
                    .time(int(item["ts"] * 1e9))
                self.write_api.write(
                    bucket=self.influx_bucket,
                    org=self.influx_org,
                    record=point,
                )
                flushed += 1
            except Exception as e:
                self.last_influx_error = str(e)
                self.influx_buffer.append(item)
                INFLUX_BUFFER_SIZE.set(len(self.influx_buffer))
                break
        if flushed:
            logger.info("influx_buffer_flushed", count=flushed)
            self._persist_full_buffer(self.influx_buffer_file, list(self.influx_buffer), enable=self.persist_influx_buffer)
            self._update_buffer_age_metrics()

    def _flush_influx_pending_batch(self):
        """将待写批次写入 Influx，失败则缓冲。"""
        if not self.influx_pending_batch:
            return
        if not self.write_api:
            for pt in self.influx_pending_batch:
                try:
                    tag = getattr(pt, "tags", {}) or {}
                    tag_name = tag.get("tag")
                    val = pt._fields.get("value")
                    unit = pt._fields.get("unit", "")
                    ts = pt._time / 1e9
                    self._buffer_influx(tag_name, val, unit, ts)
                except Exception:
                    continue
            self.influx_pending_batch.clear()
            INFLUX_PENDING_BATCH_SIZE.set(0)
            return
        try:
            self.write_api.write(
                bucket=self.influx_bucket,
                org=self.influx_org,
                record=list(self.influx_pending_batch),
            )
            self.influx_pending_batch.clear()
            INFLUX_PENDING_BATCH_SIZE.set(0)
            self._last_influx_flush_ts = time.time()
        except Exception as e:
            reason = e.__class__.__name__
            INFLUX_WRITE_FAILURE.labels(reason).inc()
            self.last_influx_error = str(e)
            logger.error("influxdb_batch_write_error", error=str(e), batch_size=len(self.influx_pending_batch))
            for pt in self.influx_pending_batch:
                try:
                    tag = getattr(pt, "tags", {}) or {}
                    tag_name = tag.get("tag")
                    val = pt._fields.get("value")
                    unit = pt._fields.get("unit", "")
                    ts = pt._time / 1e9
                    self._buffer_influx(tag_name, val, unit, ts)
                except Exception:
                    continue
            self.influx_pending_batch.clear()
            INFLUX_PENDING_BATCH_SIZE.set(0)
            self._last_influx_flush_ts = time.time()

    def _persist_buffer(self, path: Path, entry: dict, enable: bool):
        if not enable:
            return
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.warning("buffer_persist_failed", path=str(path), error=str(e))

    def _persist_full_buffer(self, path: Path, entries: list, enable: bool):
        if not enable:
            return
        try:
            with path.open("w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.warning("buffer_persist_full_failed", path=str(path), error=str(e))

    def _load_persisted_buffers(self):
        """启动时加载持久化缓冲。"""
        def load_file(path: Path, target: deque, maxlen: int):
            if not path.exists():
                return
            try:
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            target.append(obj)
                            if len(target) > maxlen:
                                target.popleft()
                        except Exception:
                            continue
            except Exception as e:
                logger.warning("buffer_load_failed", path=str(path), error=str(e))
        load_file(self.mqtt_buffer_file, self.mqtt_buffer, self.mqtt_buffer.maxlen)
        load_file(self.influx_buffer_file, self.influx_buffer, self.influx_buffer.maxlen)
        MQTT_BUFFER_SIZE.set(len(self.mqtt_buffer))
        INFLUX_BUFFER_SIZE.set(len(self.influx_buffer))
        self._update_buffer_age_metrics()

    def _update_buffer_age_metrics(self):
        now = time.time()
        if self.mqtt_buffer:
            oldest = self.mqtt_buffer[0].get("ts") or now
            MQTT_BUFFER_OLDEST_AGE_SEC.set(max(0, now - oldest))
        else:
            MQTT_BUFFER_OLDEST_AGE_SEC.set(0)
        if self.influx_buffer:
            oldest = self.influx_buffer[0].get("ts") or now
            INFLUX_BUFFER_OLDEST_AGE_SEC.set(max(0, now - oldest))
        else:
            INFLUX_BUFFER_OLDEST_AGE_SEC.set(0)

    def _check_buffer_thresholds(self):
        """缓冲水位预警，便于监控接入报警。"""
        if self.mqtt_buffer_warn > 0 and len(self.mqtt_buffer) >= self.mqtt_buffer_warn:
            if not self._buffer_warned_mqtt:
                logger.warning("mqtt_buffer_warn_threshold", size=len(self.mqtt_buffer), warn=self.mqtt_buffer_warn)
                MQTT_BUFFER_WARN.inc()
                self._buffer_warned_mqtt = True
        else:
            self._buffer_warned_mqtt = False

        if self.influx_buffer_warn > 0 and len(self.influx_buffer) >= self.influx_buffer_warn:
            if not self._buffer_warned_influx:
                logger.warning("influx_buffer_warn_threshold", size=len(self.influx_buffer), warn=self.influx_buffer_warn)
                INFLUX_BUFFER_WARN.inc()
                self._buffer_warned_influx = True
        else:
            self._buffer_warned_influx = False

    def get_status(self) -> dict:
        """返回发布通路的健康状态摘要，供健康检查使用。"""
        return {
            "mqtt_connected": self.mqtt_connected,
            "influx_connected": self.influx_connected,
            "last_mqtt_error": self.last_mqtt_error,
            "last_influx_error": self.last_influx_error,
            "mqtt_backoff": self._mqtt_reconnect_backoff,
            "mqtt_buffer_size": len(self.mqtt_buffer),
            "influx_buffer_size": len(self.influx_buffer),
        }
    
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

