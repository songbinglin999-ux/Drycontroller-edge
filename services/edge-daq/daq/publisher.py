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
import base64
import json
import os
import time
import hashlib
import errno
from typing import Optional
from collections import deque
from pathlib import Path

import paho.mqtt.client as mqtt
import structlog
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import ASYNCHRONOUS
from influxdb_client.rest import ApiException
from prometheus_client import Counter, Gauge

logger = structlog.get_logger(__name__)

MQTT_PUBLISH_FAILURE = Counter("mqtt_publish_failure_total", "MQTT publish failures", ["reason"])
MQTT_PUBLISH_RETRY = Counter("mqtt_publish_retry_total", "MQTT publish retry attempts")
MQTT_RECONNECT_TOTAL = Counter("mqtt_reconnect_total", "MQTT reconnect attempts")
MQTT_CONNECTED = Gauge("mqtt_connected", "MQTT connection state")
MQTT_ACTIVE_BROKER = Gauge("mqtt_active_broker", "MQTT active broker index (0=primary,1=backup)")

INFLUX_WRITE_FAILURE = Counter("influx_write_failure_total", "InfluxDB write failures", ["reason"])
INFLUX_WRITE_RETRY = Counter("influx_write_retry_total", "InfluxDB write retry attempts")
INFLUX_RECONNECT_TOTAL = Counter("influx_reconnect_total", "InfluxDB reconnect attempts")
INFLUX_CONNECTED = Gauge("influx_connected", "InfluxDB connection state")
INFLUX_ACTIVE_HOST = Gauge("influx_active_host", "InfluxDB active host index (0=primary,1=backup)")
MQTT_BUFFER_SIZE = Gauge("mqtt_buffer_size", "MQTT pending buffer size")
INFLUX_BUFFER_SIZE = Gauge("influx_buffer_size", "Influx pending buffer size")
MQTT_BUFFER_OLDEST_AGE_SEC = Gauge("mqtt_buffer_oldest_age_sec", "Oldest MQTT buffered message age (s)")
INFLUX_BUFFER_OLDEST_AGE_SEC = Gauge("influx_buffer_oldest_age_sec", "Oldest Influx buffered point age (s)")
INFLUX_PENDING_BATCH_SIZE = Gauge("influx_pending_batch_size", "Pending batch points before write")
INFLUX_WRITE_RATE_LIMITED = Counter("influx_write_rate_limited_total", "Influx writes deferred by rate limit")
INFLUX_TIMESTAMP_PARSE_FAILURE = Counter("influx_timestamp_parse_failure_total", "Influx timestamp parse failures", ["field"])
INFLUX_BUFFER_WARN = Counter("influx_buffer_warn_total", "Influx buffer exceeded warn threshold")
MQTT_BUFFER_WARN = Counter("mqtt_buffer_warn_total", "MQTT buffer exceeded warn threshold")
BACKPRESSURE_CALC_FAILURE = Counter("backpressure_calc_failure_total", "Backpressure calculation failures")

# 工业级运维：背压/落盘/丢弃监控
BUFFER_PERSIST_FAILURE = Counter("buffer_persist_failure_total", "Buffer persist failures", ["type", "reason"])
BUFFER_PERSIST_DISABLED = Gauge("buffer_persist_disabled", "Buffer persist disabled state (1=disabled)", ["type"])
BUFFER_DROPPED_TOTAL = Counter("buffer_dropped_total", "Buffered entries dropped due to maxlen", ["type"])
BUFFER_DEADLETTER_TOTAL = Counter("buffer_deadletter_total", "Dead-letter entries written", ["type"])
PUBLISHER_DRAIN_TIMEOUT = Counter("publisher_drain_timeout_total", "Publisher drain timed out")
PUBLISHER_DRAIN_SUCCESS = Counter("publisher_drain_success_total", "Publisher drain success")


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
        self._influx_reconnect_backoff = 1.0
        self._influx_reconnect_backoff_max = 30.0
        self.mqtt_connected = False
        self.influx_connected = False
        self.last_mqtt_error: Optional[str] = None
        self.last_influx_error: Optional[str] = None
        self.last_backpressure_error: Optional[str] = None
        self.last_buffer_error: Optional[str] = None
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
        # MQTT 备链路（故障切换）
        self.mqtt_broker_backup = os.getenv("MQTT_BROKER_BACKUP", "")
        self.mqtt_port_backup = int(os.getenv("MQTT_PORT_BACKUP", self.mqtt_port) or self.mqtt_port)
        self._active_mqtt_is_backup = False
        
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
        default_dead_letter = self.buffer_dir / "deadletter"
        self.dead_letter_dir = Path(os.getenv("DEAD_LETTER_DIR", str(default_dead_letter)))
        self.dead_letter_dir.mkdir(parents=True, exist_ok=True)
        self.mqtt_dead_letter = self.dead_letter_dir / "mqtt_deadletter.jsonl"
        self.influx_dead_letter = self.dead_letter_dir / "influx_deadletter.jsonl"
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
        self._active_influx_is_backup = False

        # 幂等/去重：给每条数据生成稳定 id（下游可用来去重）
        # 注意：QoS1/2 仍可能导致“至少一次”投递；id 用于消费端幂等处理
        self.id_prefix = os.getenv("PUBLISH_ID_PREFIX", self.line_id)

        # 磁盘保护：低剩余空间/磁盘满时停止落盘，避免打爆节点
        self.buffer_min_free_bytes = int(os.getenv("BUFFER_MIN_FREE_BYTES", "0") or "0")
        self.disable_persist_on_enospc = os.getenv("DISABLE_PERSIST_ON_ENOSPC", "true").lower() == "true"
        self._persist_disabled = {"mqtt": False, "influx": False}

        # 背压：采集侧可读取该建议延迟以适度放慢（避免下游长期不可用导致缓冲爆炸）
        self.backpressure_enabled = os.getenv("BACKPRESSURE_ENABLED", "true").lower() == "true"
        self.backpressure_max_delay_sec = float(os.getenv("BACKPRESSURE_MAX_DELAY_SEC", "1.0") or "1.0")
        self.backpressure_ratio_threshold = float(os.getenv("BACKPRESSURE_RATIO_THRESHOLD", "0.9") or "0.9")

    def get_backpressure_delay(self) -> float:
        """
        给采集侧的“建议延迟”（秒）。当缓冲接近满时返回一个小延迟。
        """
        if not self.backpressure_enabled:
            return 0.0
        try:
            mqtt_ratio = len(self.mqtt_buffer) / float(self.mqtt_buffer.maxlen or 1)
            influx_ratio = len(self.influx_buffer) / float(self.influx_buffer.maxlen or 1)
            ratio = max(mqtt_ratio, influx_ratio)
            if ratio < self.backpressure_ratio_threshold:
                return 0.0
            span = max(1e-6, (1.0 - self.backpressure_ratio_threshold))
            return max(
                0.0,
                min(
                    self.backpressure_max_delay_sec,
                    (ratio - self.backpressure_ratio_threshold) / span * self.backpressure_max_delay_sec,
                ),
            )
        except (TypeError, ValueError, ZeroDivisionError) as e:
            self.last_backpressure_error = str(e)
            BACKPRESSURE_CALC_FAILURE.inc()
            logger.error("backpressure_calculation_failed", error=str(e), exc_info=True)
            return max(0.0, self.backpressure_max_delay_sec)

    def _disk_free_bytes(self) -> int:
        try:
            st = os.statvfs(str(self.buffer_dir))
            return int(st.f_bavail) * int(st.f_frsize)
        except OSError as e:
            self.last_buffer_error = str(e)
            logger.error("buffer_disk_free_check_failed", path=str(self.buffer_dir), error=str(e), exc_info=True)
            return -1

    def _buffer_has_min_disk_space(self, buf_type: str) -> bool:
        if self.buffer_min_free_bytes <= 0:
            return True
        free_b = self._disk_free_bytes()
        if free_b < 0:
            BUFFER_PERSIST_FAILURE.labels(buf_type, "statvfs").inc()
            BUFFER_PERSIST_DISABLED.labels(buf_type).set(1)
            self._persist_disabled[buf_type] = True
            logger.error(
                "buffer_persist_disabled_disk_check_failed",
                type=buf_type,
                min_free_bytes=self.buffer_min_free_bytes,
            )
            return False
        if free_b < self.buffer_min_free_bytes:
            BUFFER_PERSIST_FAILURE.labels(buf_type, "low_disk").inc()
            BUFFER_PERSIST_DISABLED.labels(buf_type).set(1)
            self._persist_disabled[buf_type] = True
            logger.error(
                "buffer_persist_disabled_low_disk",
                type=buf_type,
                free_bytes=free_b,
                min_free_bytes=self.buffer_min_free_bytes,
            )
            return False
        return True

    def _make_msg_id(self, kind: str, tag: str, ts_ns: int) -> str:
        """
        生成稳定消息 ID，避免 payload 过长：sha1(kind:line:tag:ts_ns) 截断。
        """
        base = f"{kind}:{self.id_prefix}:{tag}:{ts_ns}"
        return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]

    def _to_ts_ns(self, timestamp: Optional[float]) -> int:
        """
        将秒级时间戳（float）转换为纳秒（int）。
        若 timestamp 为 None，使用 time.time_ns()。
        """
        if timestamp is None:
            return time.time_ns()
        try:
            return int(float(timestamp) * 1_000_000_000)
        except (TypeError, ValueError, OverflowError) as e:
            self.last_influx_error = f"invalid_timestamp: {timestamp!r}"
            logger.error("timestamp_conversion_failed", timestamp=repr(timestamp), error=str(e))
            raise ValueError(f"invalid timestamp for tag data: {timestamp!r}") from e

    def _influx_entry_ts_sec(self, entry: dict) -> float:
        """
        兼容旧/新落盘格式：
        - 新：ts_ns=int
        - 旧：ts=float(seconds) 或 ts=int(seconds)
        """
        if not entry:
            return time.time()
        ts_ns = entry.get("ts_ns")
        if ts_ns is not None:
            try:
                return float(int(ts_ns)) / 1_000_000_000
            except (TypeError, ValueError, OverflowError) as e:
                self._record_influx_timestamp_parse_failure("ts_ns", ts_ns, e)
        ts = entry.get("ts")
        try:
            ts_f = float(ts)
            # 兼容：若旧 entry 错把纳秒塞进 ts，则按纳秒处理
            if ts_f > 1_000_000_000_000:  # ~2001-09-09 之后的纳秒量级
                return ts_f / 1_000_000_000
            return ts_f
        except (TypeError, ValueError, OverflowError) as e:
            self._record_influx_timestamp_parse_failure("ts", ts, e)
            return time.time()

    def _record_influx_timestamp_parse_failure(self, field: str, value: object, exc: Exception) -> None:
        self.last_influx_error = f"invalid_buffer_timestamp_{field}: {value!r}"
        INFLUX_TIMESTAMP_PARSE_FAILURE.labels(field).inc()
        logger.warning(
            "influx_buffer_timestamp_parse_failed",
            field=field,
            value=repr(value),
            error=str(exc),
        )

    def _to_ts_ns_any(self, ts: object) -> int:
        """
        兼容输入：
        - None -> time.time_ns()
        - float/int(秒) -> 转 ns
        - int(ns) -> 原样（通过数量级判断）
        """
        if ts is None:
            return time.time_ns()
        try:
            if isinstance(ts, int):
                # 粗略判断：>1e12 基本可视为 ns
                if ts > 1_000_000_000_000:
                    return int(ts)
                return int(ts) * 1_000_000_000
            # float seconds
            return int(float(ts) * 1_000_000_000)
        except (TypeError, ValueError, OverflowError):
            return time.time_ns()
    
    async def connect(self):
        """
        建立所有连接
        
        同时连接 MQTT Broker 和 InfluxDB
        """
        # 标记 publisher 已启动（即使后端暂时不可用，也让缓冲/重放循环运行）
        self.connected = True

        # 连接 MQTT
        await self._connect_mqtt()
        
        # 连接 InfluxDB
        await self._connect_influxdb()

        # 注意：connected 表示 publisher 服务已启动（flush loop 等后台任务运行）
        # 具体通路是否已连通请看 mqtt_connected/influx_connected 或 get_status()
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
                # Influx：若有待写/缓冲，则先确保连接可用
                if self.influx_pending_batch or self.influx_buffer:
                    await self._ensure_influx()
                # MQTT：若有缓冲，则尝试确保连接可用（避免 flush 时才发现离线）
                if self.mqtt_buffer:
                    await self._ensure_mqtt()

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
            def _connect(broker: str, port: int, is_backup: bool):
                cli = mqtt.Client(client_id="edge-daq")
                if self.mqtt_user and self.mqtt_password:
                    cli.username_pw_set(self.mqtt_user, self.mqtt_password)
                if self.mqtt_tls:
                    cli.tls_set()
                    if self.mqtt_tls_insecure:
                        cli.tls_insecure_set(True)
                cli.on_connect = self._on_mqtt_connect
                cli.on_disconnect = self._on_mqtt_disconnect
                cli.connect(broker, port, keepalive=60)
                cli.loop_start()
                self._active_mqtt_is_backup = is_backup
                MQTT_ACTIVE_BROKER.set(1 if is_backup else 0)
                logger.info(
                    "mqtt_publisher_connected",
                    broker=broker,
                    port=port,
                    active="backup" if is_backup else "primary",
                )
                return cli

            try:
                self.mqtt_client = _connect(self.mqtt_broker, self.mqtt_port, is_backup=False)
            except (OSError, RuntimeError, ValueError) as primary_err:
                if not self.mqtt_broker_backup:
                    raise
                logger.warning("mqtt_primary_connect_failed_try_backup", error=str(primary_err))
                self.mqtt_client = _connect(self.mqtt_broker_backup, self.mqtt_port_backup, is_backup=True)
        
        except (OSError, RuntimeError, ValueError) as e:
            logger.error("mqtt_connect_error", error=str(e))
            self.last_mqtt_error = str(e)
            self.mqtt_connected = False
            MQTT_CONNECTED.set(0)
    
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
                # health() 是同步 IO，放到线程池避免阻塞事件循环
                await asyncio.to_thread(client.health)
                self._active_influx_is_backup = False
                INFLUX_ACTIVE_HOST.set(0)
            except (ApiException, OSError, RuntimeError, ValueError) as primary_err:
                if not self.influx_host_backup:
                    raise primary_err
                logger.warning("influx_primary_connect_failed_try_backup", error=str(primary_err))
                url, client = _connect_one(
                    self.influx_host_backup,
                    self.influx_port_backup or self.influx_port,
                    self.influx_scheme_backup,
                )
                await asyncio.to_thread(client.health)
                self._active_influx_is_backup = True
                INFLUX_ACTIVE_HOST.set(1)

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

        except (ApiException, OSError, RuntimeError, ValueError) as e:
            logger.error("influxdb_connect_error", error=str(e))
            self.last_influx_error = str(e)
            INFLUX_CONNECTED.set(0)
            self.influx_connected = False
            self.influx_client = None
            self.write_api = None

    async def _ensure_mqtt(self):
        """
        确保 MQTT 客户端处于连接状态，失败时按指数退避重连。
        """
        if self.mqtt_client and getattr(self.mqtt_client, "is_connected", lambda: False)():
            # 已连接则重置退避，避免后续不必要的长等待
            self._mqtt_reconnect_backoff = 1.0
            return True
        backoff = self._mqtt_reconnect_backoff
        await self._connect_mqtt()
        # 逐步增大退避时间，避免连接风暴
        self._mqtt_reconnect_backoff = min(self._mqtt_reconnect_backoff * 2, self._mqtt_reconnect_backoff_max)
        if self.mqtt_client and getattr(self.mqtt_client, "is_connected", lambda: False)():
            self._mqtt_reconnect_backoff = 1.0
            return True
        # 等待退避时间后由调用方决定是否继续
        await asyncio.sleep(backoff)
        return False

    async def _ensure_influx(self):
        """
        确保 InfluxDB 客户端可用，失败时按指数退避重连。
        """
        if self.influx_client and self.write_api:
            try:
                await asyncio.to_thread(self.influx_client.health)
                self._influx_reconnect_backoff = 1.0
                if not self.influx_connected:
                    INFLUX_RECONNECT_TOTAL.inc()
                self.influx_connected = True
                INFLUX_CONNECTED.set(1)
                return True
            except (ApiException, OSError, RuntimeError, ValueError) as e:
                self.last_influx_error = str(e)
                self.influx_connected = False
                INFLUX_CONNECTED.set(0)

        backoff = self._influx_reconnect_backoff
        await self._connect_influxdb()
        self._influx_reconnect_backoff = min(self._influx_reconnect_backoff * 2, self._influx_reconnect_backoff_max)
        if self.influx_client and self.write_api and self.influx_connected:
            self._influx_reconnect_backoff = 1.0
            INFLUX_RECONNECT_TOTAL.inc()
            return True
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
    
    async def publish_tag_data(
        self,
        tag_name: str,
        value: float,
        unit: str = "",
        timestamp: Optional[float] = None,
        quality: str = "good",
    ):
        """
        发布标签数据
        
        同时将数据发送到 MQTT（实时推送）和 InfluxDB（历史存储）
        
        Args:
            tag_name: 标签名称（如 FTT1）
            value: 采集到的数值
            unit: 工程单位（如 ℃、%）
            timestamp: 时间戳（默认当前时间）
        """
        ts_ns = self._to_ts_ns(timestamp)
        ts_sec = int(ts_ns // 1_000_000_000)
        msg_id = self._make_msg_id("tag", tag_name, ts_ns)
        
        # 构造消息数据
        data = {
            "line": self.line_id,   # 产线标识
            "tag": tag_name,         # 标签名
            "value": value,          # 数值
            "unit": unit,            # 单位
            "quality": quality,      # 采集质量
            "ts": ts_sec,            # 时间戳（秒）
            "ts_ns": ts_ns,          # 时间戳（纳秒）
            "id": msg_id,            # 幂等 ID（下游可用于去重）
        }
        
        # 发布到 MQTT（供实时订阅者消费）
        topic = self.realtime_topic
        await self._publish_mqtt(topic, data, qos=1)
        
        # 写入 InfluxDB（供历史查询）
        self._write_influxdb(tag_name, value, unit, ts_ns, quality)
    
    async def _publish_mqtt(self, topic: str, data: dict, qos: int = 1):
        """
        发布消息到 MQTT 主题
        
        Args:
            topic: 目标主题
            data: 消息数据（字典）
            qos: 服务质量等级（默认 1）
        
        工业现场要求采集侧尽量不被发布侧阻塞：
        - 热路径只做一次 best-effort publish
        - 失败立即入缓冲（可落盘）并由后台 flush loop 负责重放
        """
        # 未初始化或未连通：不要丢数据，直接缓冲并触发后台重连
        if not (self.mqtt_client and getattr(self.mqtt_client, "is_connected", lambda: False)()):
            self._buffer_mqtt(topic, data, qos)
            try:
                # 只有在 publisher 已启动（connect() 之后）才启动后台重连；
                # 否则（如单测/离线工具）只缓冲不产生悬挂任务。
                if self.connected:
                    asyncio.get_running_loop().create_task(self._ensure_mqtt())
            except RuntimeError:
                # 没有运行中的 loop（例如在同步上下文里被调用），就不在此处阻塞重连
                pass
            return

        try:
            payload = json.dumps(data)
            result = self.mqtt_client.publish(topic, payload, qos)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                return
            reason = str(result.rc)
            MQTT_PUBLISH_FAILURE.labels(reason).inc()
            logger.error("mqtt_publish_failed", topic=topic, rc=result.rc)
        except (OSError, RuntimeError, TypeError, ValueError) as e:
            reason = e.__class__.__name__
            MQTT_PUBLISH_FAILURE.labels(reason).inc()
            self.last_mqtt_error = str(e)
            logger.error("mqtt_publish_error", topic=topic, error=str(e))

        # 失败：入缓冲 + 异步触发重连（避免阻塞采集循环）
        self._buffer_mqtt(topic, data, qos)
        MQTT_PUBLISH_RETRY.inc()
        try:
            if self.connected:
                asyncio.get_running_loop().create_task(self._ensure_mqtt())
        except RuntimeError:
            pass
    
    def _write_influxdb(
        self,
        tag_name: str,
        value: float,
        unit: str,
        ts_ns: int,
        quality: str = "good",
        retries: int = 2,
    ):
        """
        写入数据到 InfluxDB
        
        使用 Line Protocol 格式写入时序数据点
        
        Args:
            tag_name: 标签名（作为 tag）
            value: 数值（作为 field）
            unit: 单位（作为 field）
            ts_ns: 时间戳（纳秒）
        """
        if not self.write_api:
            self._buffer_influx(tag_name, value, unit, ts_ns, quality)
            return

        now_ns = time.time_ns()
        if self.ts_max_skew_sec > 0:
            skew_sec = abs(now_ns - int(ts_ns)) / 1_000_000_000
            if skew_sec > self.ts_max_skew_sec:
                logger.warning("ts_skew_detected", tag=tag_name, ts_ns=ts_ns, now_ns=now_ns, skew_sec=skew_sec)

        # 速率限制：超过速率则缓冲，避免打爆写入
        now = time.time()
        if self.influx_max_points_per_sec > 0:
            if now - self._rate_window_start >= 1:
                self._rate_window_start = now
                self._rate_points = 0
            if self._rate_points >= self.influx_max_points_per_sec:
                INFLUX_WRITE_RATE_LIMITED.inc()
                self._buffer_influx(tag_name, value, unit, ts_ns, quality)
                return
            self._rate_points += 1

        point = Point(self.measurement) \
            .tag("tag", tag_name) \
            .field("value", float(value)) \
            .field("unit", unit) \
            .field("quality", quality) \
            .time(int(ts_ns))  # 直接使用纳秒时间戳（幂等）

        self.influx_pending_batch.append(point)
        INFLUX_PENDING_BATCH_SIZE.set(len(self.influx_pending_batch))

        if len(self.influx_pending_batch) >= self.influx_batch_max or (now - self._last_influx_flush_ts) >= self.influx_flush_interval:
            self._flush_influx_pending_batch()

    def _buffer_mqtt(self, topic: str, data: dict, qos: int):
        """将 MQTT 消息加入缓冲，超过上限会丢弃最旧并告警。"""
        if len(self.mqtt_buffer) >= self.mqtt_buffer.maxlen:
            oldest = self.mqtt_buffer.popleft()
            logger.warning("mqtt_buffer_dropped_oldest", maxlen=self.mqtt_buffer.maxlen)
            BUFFER_DROPPED_TOTAL.labels("mqtt").inc()
            self._dead_letter(self.mqtt_dead_letter, oldest)
        entry = {"topic": topic, "data": data, "qos": qos, "ts": int(time.time())}
        self.mqtt_buffer.append(entry)
        MQTT_BUFFER_SIZE.set(len(self.mqtt_buffer))
        self._persist_buffer(self.mqtt_buffer_file, entry, enable=self.persist_mqtt_buffer)
        self._update_buffer_age_metrics()
        self._check_buffer_thresholds()

    def _buffer_influx(
        self, tag_name: str, value: float, unit: str, timestamp, quality: str = "good"
    ):
        """将 Influx 数据加入缓冲。"""
        if len(self.influx_buffer) >= self.influx_buffer.maxlen:
            oldest = self.influx_buffer.popleft()
            logger.warning("influx_buffer_dropped_oldest", maxlen=self.influx_buffer.maxlen)
            BUFFER_DROPPED_TOTAL.labels("influx").inc()
            self._dead_letter(self.influx_dead_letter, oldest)
        # 统一使用 ts_ns，保证重放写入幂等（相同 measurement+tag+timestamp 会覆盖）
        ts_ns = self._to_ts_ns_any(timestamp)
        entry = {
            "tag": tag_name,
            "value": value,
            "unit": unit,
            "quality": quality,
            "ts_ns": int(ts_ns),
        }
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
            except (OSError, RuntimeError, TypeError, ValueError) as e:
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
                ts_sec = self._influx_entry_ts_sec(item)
                ts_ns = item.get("ts_ns")
                if ts_ns is None:
                    ts_ns = int(ts_sec * 1_000_000_000)
                point = Point(self.measurement) \
                    .tag("tag", item["tag"]) \
                    .field("value", float(item["value"])) \
                    .field("unit", item["unit"]) \
                    .field("quality", item["quality"]) \
                    .time(int(ts_ns))
                self.write_api.write(
                    bucket=self.influx_bucket,
                    org=self.influx_org,
                    record=point,
                )
                flushed += 1
            except (ApiException, AttributeError, OSError, RuntimeError, TypeError, ValueError) as e:
                self.last_influx_error = str(e)
                self.influx_connected = False
                INFLUX_CONNECTED.set(0)
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
                    quality = pt._fields.get("quality", "good")
                    # pt._time 可能是纳秒或 datetime；兜底用当前时间
                    ts_ns = None
                    try:
                        if isinstance(pt._time, int):
                            ts_ns = int(pt._time)
                    except (TypeError, ValueError):
                        ts_ns = None
                    if ts_ns is None:
                        ts_ns = time.time_ns()
                    self._buffer_influx(tag_name, val, unit, ts_ns, quality)
                except (AttributeError, TypeError, ValueError):
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
        except (ApiException, OSError, RuntimeError, TypeError, ValueError) as e:
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
                    quality = pt._fields.get("quality", "good")
                    ts_ns = None
                    try:
                        if isinstance(pt._time, int):
                            ts_ns = int(pt._time)
                    except (TypeError, ValueError):
                        ts_ns = None
                    if ts_ns is None:
                        ts_ns = time.time_ns()
                    self._buffer_influx(tag_name, val, unit, ts_ns, quality)
                except (AttributeError, TypeError, ValueError):
                    continue
            self.influx_pending_batch.clear()
            INFLUX_PENDING_BATCH_SIZE.set(0)
            self._last_influx_flush_ts = time.time()

    def _persist_buffer(self, path: Path, entry: dict, enable: bool):
        if not enable:
            return
        buf_type = "mqtt" if "mqtt" in path.name else "influx"
        if self._persist_disabled.get(buf_type):
            BUFFER_PERSIST_DISABLED.labels(buf_type).set(1)
            return
        if not self._buffer_has_min_disk_space(buf_type):
            return
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except (OSError, TypeError, ValueError) as e:
            reason = e.__class__.__name__
            if isinstance(e, OSError) and getattr(e, "errno", None) == errno.ENOSPC:
                reason = "ENOSPC"
                if self.disable_persist_on_enospc:
                    self._persist_disabled[buf_type] = True
                    BUFFER_PERSIST_DISABLED.labels(buf_type).set(1)
            logger.warning("buffer_persist_failed", path=str(path), error=str(e))
            BUFFER_PERSIST_FAILURE.labels(buf_type, reason).inc()

    def _persist_full_buffer(self, path: Path, entries: list, enable: bool):
        if not enable:
            return
        buf_type = "mqtt" if "mqtt" in path.name else "influx"
        if self._persist_disabled.get(buf_type):
            BUFFER_PERSIST_DISABLED.labels(buf_type).set(1)
            return
        if not self._buffer_has_min_disk_space(buf_type):
            return
        try:
            with path.open("w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry) + "\n")
        except (OSError, TypeError, ValueError) as e:
            reason = e.__class__.__name__
            if isinstance(e, OSError) and getattr(e, "errno", None) == errno.ENOSPC:
                reason = "ENOSPC"
                if self.disable_persist_on_enospc:
                    self._persist_disabled[buf_type] = True
                    BUFFER_PERSIST_DISABLED.labels(buf_type).set(1)
            logger.warning("buffer_persist_full_failed", path=str(path), error=str(e))
            BUFFER_PERSIST_FAILURE.labels(buf_type, reason).inc()

    def _load_persisted_buffers(self):
        """启动时加载持久化缓冲。"""
        def valid_mqtt(entry: object) -> bool:
            return (
                isinstance(entry, dict)
                and set(entry) == {"topic", "data", "qos", "ts"}
                and isinstance(entry["topic"], str)
                and bool(entry["topic"])
                and isinstance(entry["data"], dict)
                and isinstance(entry["qos"], int)
                and not isinstance(entry["qos"], bool)
                and entry["qos"] in {0, 1, 2}
                and isinstance(entry["ts"], (int, float))
                and not isinstance(entry["ts"], bool)
            )

        def valid_influx(entry: object) -> bool:
            return (
                isinstance(entry, dict)
                and set(entry) == {"tag", "value", "unit", "quality", "ts_ns"}
                and isinstance(entry["tag"], str)
                and bool(entry["tag"])
                and isinstance(entry["value"], (int, float))
                and not isinstance(entry["value"], bool)
                and isinstance(entry["unit"], str)
                and isinstance(entry["quality"], str)
                and bool(entry["quality"])
                and isinstance(entry["ts_ns"], int)
                and not isinstance(entry["ts_ns"], bool)
            )

        def load_file(path: Path, target: deque, maxlen: int, validator, dead_letter: Path):
            if not path.exists():
                return
            try:
                with path.open("rb") as f:
                    for raw_line in f:
                        try:
                            line = raw_line.decode("utf-8").strip()
                        except UnicodeDecodeError:
                            self._dead_letter(
                                dead_letter,
                                {
                                    "reason": "invalid_persisted_buffer_utf8",
                                    "raw_line_base64": base64.b64encode(raw_line).decode("ascii"),
                                },
                            )
                            continue
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            if not validator(obj):
                                self._dead_letter(
                                    dead_letter,
                                    {
                                        "reason": "invalid_persisted_buffer_schema",
                                        "entry": obj,
                                    },
                                )
                                continue
                            target.append(obj)
                            if len(target) > maxlen:
                                target.popleft()
                        except (json.JSONDecodeError, TypeError):
                            self._dead_letter(
                                dead_letter,
                                {
                                    "reason": "invalid_persisted_buffer_json",
                                    "entry": line,
                                },
                            )
                            continue
            except OSError as e:
                logger.warning("buffer_load_failed", path=str(path), error=str(e))
        load_file(
            self.mqtt_buffer_file,
            self.mqtt_buffer,
            self.mqtt_buffer.maxlen,
            valid_mqtt,
            self.mqtt_dead_letter,
        )
        load_file(
            self.influx_buffer_file,
            self.influx_buffer,
            self.influx_buffer.maxlen,
            valid_influx,
            self.influx_dead_letter,
        )
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
            oldest_sec = self._influx_entry_ts_sec(self.influx_buffer[0])
            INFLUX_BUFFER_OLDEST_AGE_SEC.set(max(0, now - oldest_sec))
        else:
            INFLUX_BUFFER_OLDEST_AGE_SEC.set(0)

    def _dead_letter(self, path: Path, entry: dict):
        """将被丢弃的缓冲写入死信文件，便于事后分析。"""
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
                f.flush()
                os.fsync(f.fileno())
            if "mqtt" in path.name:
                BUFFER_DEADLETTER_TOTAL.labels("mqtt").inc()
            else:
                BUFFER_DEADLETTER_TOTAL.labels("influx").inc()
        except (OSError, TypeError, ValueError) as e:
            self.last_buffer_error = f"dead_letter_write_failed: {e}"
            logger.error("dead_letter_write_failed", path=str(path), error=str(e))
            raise RuntimeError(f"dead-letter persistence failed: {path}") from e

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
            "last_backpressure_error": self.last_backpressure_error,
            "last_buffer_error": self.last_buffer_error,
            "mqtt_backoff": self._mqtt_reconnect_backoff,
            "mqtt_buffer_size": len(self.mqtt_buffer),
            "influx_buffer_size": len(self.influx_buffer),
            "mqtt_active_broker": "backup" if self._active_mqtt_is_backup else "primary",
            "influx_active_host": "backup" if self._active_influx_is_backup else "primary",
            "persist_disabled": dict(self._persist_disabled),
        }

    async def drain(self, timeout_sec: float = 10.0, interval_sec: float = 0.2) -> bool:
        """
        停机前尽力清空缓冲队列（超时可控）。
        """
        deadline = time.monotonic() + max(0.0, float(timeout_sec or 0.0))
        while time.monotonic() < deadline:
            pending = bool(self.mqtt_buffer) or bool(self.influx_buffer) or bool(self.influx_pending_batch)
            if not pending:
                PUBLISHER_DRAIN_SUCCESS.inc()
                return True

            if self.mqtt_buffer:
                await self._ensure_mqtt()
            if self.influx_buffer or self.influx_pending_batch:
                await self._ensure_influx()

            self._flush_influx_pending_batch()
            await self.flush_buffers()
            await asyncio.sleep(interval_sec)

        PUBLISHER_DRAIN_TIMEOUT.inc()
        return False
    
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
        ts_ns = self._to_ts_ns(None)
        msg_id = self._make_msg_id(f"alarm:{alarm_type}", tag_name, ts_ns)

        alarm_data = {
            "line": self.line_id,
            "tag": tag_name,
            "type": alarm_type,
            "value": value,
            "threshold": threshold,
            "ts": int(ts_ns // 1_000_000_000),
            "ts_ns": ts_ns,
            "id": msg_id,
        }
        
        # 使用 QoS=2 确保报警消息可靠送达（至少一次送达语义；最终可靠性由缓冲重放兜底）
        topic = self.alarm_topic
        await self._publish_mqtt(topic, alarm_data, qos=2)
        
        logger.warning(
            "alarm_published",
            tag=tag_name,
            type=alarm_type,
            value=value,
            threshold=threshold,
        )
