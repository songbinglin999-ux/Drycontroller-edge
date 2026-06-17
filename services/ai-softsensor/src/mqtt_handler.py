"""
MQTT 消息处理器
"""

from __future__ import annotations

import asyncio
import json
import math
from collections import deque
import structlog
import paho.mqtt.client as mqtt
import os
import threading
import time
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import ASYNCHRONOUS
from prometheus_client import Counter, Gauge

logger = structlog.get_logger(__name__)

MQTT_RX_TOTAL = Counter("softsensor_mqtt_rx_total", "Softsensor realtime MQTT messages received")
MQTT_RX_PARSE_ERROR_TOTAL = Counter("softsensor_mqtt_rx_parse_error_total", "Softsensor realtime MQTT parse errors")
MQTT_RX_INVALID_VALUE_TOTAL = Counter("softsensor_mqtt_rx_invalid_value_total", "Softsensor realtime MQTT invalid values")

FEATURE_MISSING_TOTAL = Counter("softsensor_feature_missing_total", "Inference skipped due to missing features", ["feature"])
FEATURE_STALE_TOTAL = Counter("softsensor_feature_stale_total", "Inference skipped due to stale features", ["feature"])
FEATURE_FROZEN_TOTAL = Counter("softsensor_feature_frozen_total", "Feature appears frozen (no change beyond threshold)", ["feature"])

INFERENCE_SCHEDULED_TOTAL = Counter("softsensor_inference_scheduled_total", "Inference scheduled count")
INFERENCE_SKIPPED_THROTTLE_TOTAL = Counter("softsensor_inference_skipped_throttle_total", "Inference skipped due to throttle")
INFERENCE_INFLIGHT_SKIPPED_TOTAL = Counter("softsensor_inference_inflight_skipped_total", "Inference skipped because previous is still running")
INFERENCE_FAILED_TOTAL = Counter("softsensor_inference_failed_total", "Inference failed count")
INFERENCE_DURATION_MS = Gauge("softsensor_inference_duration_ms", "Last inference duration (ms)")
INFERENCE_LAST_TS = Gauge("softsensor_inference_last_ts", "Last successful inference unix timestamp (sec)")
FEATURE_MAX_AGE_SEC = Gauge("softsensor_feature_max_age_sec", "Max age of features used for inference (sec)")

MQTT_PUBLISH_FAILURE_TOTAL = Counter("softsensor_ai_publish_failure_total", "AI publish failures (to MQTT)", ["reason"])
MQTT_OUTBOX_DROPPED_TOTAL = Counter("softsensor_ai_outbox_dropped_total", "AI outbox drops due to maxlen")
MQTT_OUTBOX_SIZE = Gauge("softsensor_ai_outbox_size", "AI outbox pending size")

INFLUX_WRITE_FAILURE_TOTAL = Counter("softsensor_influx_write_failure_total", "Influx write failures", ["reason"])
INFLUX_BUFFER_DROPPED_TOTAL = Counter("softsensor_influx_buffer_dropped_total", "Influx buffer drops due to maxlen")
INFLUX_BUFFER_SIZE = Gauge("softsensor_influx_buffer_size", "Influx pending points buffer size")
INFLUX_CONNECTED = Gauge("softsensor_influx_connected", "Influx client available (1=connected)")


class MQTTHandler:
    """MQTT 处理器"""
    
    def __init__(self, model, config):
        self.model = model
        self.config = config
        self.client = None
        self.connected = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._maintenance_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._inference_inflight = False

        self.influx_client = None
        self.write_api = None
        # 注意：paho-mqtt 回调运行在网络线程，需要线程安全
        self._lock = threading.Lock()
        self.data_cache: dict[str, float] = {}
        self._tag_ts_sec: dict[str, float] = {}  # 产线数据时间戳（秒）
        self._last_value: dict[str, float] = {}
        self._last_change_ts: dict[str, float] = {}
        self._last_inference_ts: float = 0.0
        self._last_feature_max_age_sec: float = 0.0
        self._last_frozen_features: list[str] = []
        
        softsensor_cfg = self.config.get("softsensor", {})
        
        self.mqtt_broker = os.getenv("MQTT_BROKER", "localhost")
        self.mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
        self.mqtt_user = os.getenv("MQTT_USER", "")
        self.mqtt_password = os.getenv("MQTT_PASSWORD", "")
        self.line_id = os.getenv("LINE_ID", "L1")
        self.model_version = softsensor_cfg.get("model_version", "v1.0.0")
        self.inference_source = softsensor_cfg.get("inference_source", "ai-softsensor")
        batch_id_keys = softsensor_cfg.get("batch_id_keys", ["BATCH.ID", "batch_id", "BATCH_ID"])
        if isinstance(batch_id_keys, str):
            batch_id_keys = [batch_id_keys]
        self.batch_id_keys = batch_id_keys
        
        publish_cfg = softsensor_cfg.get("publish", {})
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
        self.training_measurement = os.getenv("INFLUXDB_TRAIN_MEASUREMENT", "softsensor_training_data")

        # ---------- 数据质量与长期运行保护 ----------
        self.max_feature_age_sec = float(os.getenv("SOFTSENSOR_MAX_FEATURE_AGE_SEC", "10") or "10")
        self.freeze_sec = float(os.getenv("SOFTSENSOR_FREEZE_SEC", "120") or "120")
        self.inference_min_interval_sec = float(os.getenv("SOFTSENSOR_INFERENCE_MIN_INTERVAL_SEC", "1.0") or "1.0")
        # readiness 的“数据质量门控”参数（用于告警/运维展示链路）
        self.ready_require_recent_inference = os.getenv("SOFTSENSOR_READY_REQUIRE_RECENT_INFERENCE", "true").lower() == "true"
        self.ready_max_inference_age_sec = float(os.getenv("SOFTSENSOR_READY_MAX_INFERENCE_AGE_SEC", "60") or "60")
        self.ready_max_feature_age_sec = float(os.getenv("SOFTSENSOR_READY_MAX_FEATURE_AGE_SEC", str(self.max_feature_age_sec)) or str(self.max_feature_age_sec))
        self.ready_max_frozen_features = int(os.getenv("SOFTSENSOR_READY_MAX_FROZEN_FEATURES", "2") or "2")

        self.enable_mqtt_outbox = os.getenv("SOFTSENSOR_MQTT_OUTBOX_ENABLED", "true").lower() == "true"
        self.mqtt_outbox = deque(maxlen=int(os.getenv("SOFTSENSOR_MQTT_OUTBOX_MAX", "500") or "500"))

        self.enable_influx_buffer = os.getenv("SOFTSENSOR_INFLUX_BUFFER_ENABLED", "true").lower() == "true"
        self.influx_buffer = deque(maxlen=int(os.getenv("SOFTSENSOR_INFLUX_BUFFER_MAX", "2000") or "2000"))
        self.flush_interval_sec = float(os.getenv("SOFTSENSOR_FLUSH_INTERVAL_SEC", "2") or "2")
        self._influx_reconnect_backoff = 1.0
        self._influx_reconnect_backoff_max = float(os.getenv("SOFTSENSOR_INFLUX_RECONNECT_MAX_SEC", "30") or "30")
    
    async def connect(self):
        """连接 MQTT"""
        self._stop_event = asyncio.Event()
        try:
            self._loop = asyncio.get_running_loop()
        except Exception:
            self._loop = None
        self.client = mqtt.Client(client_id="ai-softsensor")
        
        if self.mqtt_user and self.mqtt_password:
            self.client.username_pw_set(self.mqtt_user, self.mqtt_password)
        
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        # 自动重连退避：应对 broker 抖动
        try:
            if hasattr(self.client, "reconnect_delay_set"):
                self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        except Exception:
            pass
        
        self.client.connect_async(self.mqtt_broker, self.mqtt_port, keepalive=60)
        self.client.loop_start()
        self._connect_influx()

        # 启动后台维护循环：flush outbox/缓冲、Influx 自修复
        if self._loop and not self._maintenance_task:
            self._maintenance_task = self._loop.create_task(self._maintenance_loop())
        
        logger.info("mqtt_handler_connected")
    
    async def disconnect(self):
        """断开 MQTT"""
        try:
            self._stop_event.set()
        except Exception:
            pass
        if self._maintenance_task:
            self._maintenance_task.cancel()
            self._maintenance_task = None

        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self.connected = False
        
        if self.influx_client:
            self.influx_client.close()
        self.influx_client = None
        self.write_api = None
        INFLUX_CONNECTED.set(0)
        
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
            INFLUX_CONNECTED.set(1)
        except Exception as exc:
            logger.error("softsensor_influx_connect_failed", error=str(exc))
            self.influx_client = None
            self.write_api = None
            INFLUX_CONNECTED.set(0)

    async def _ensure_influx(self) -> bool:
        """
        尽力确保 Influx 写入通路可用：失败时指数退避重连。
        """
        if self.influx_client and self.write_api:
            INFLUX_CONNECTED.set(1)
            self._influx_reconnect_backoff = 1.0
            return True
        INFLUX_CONNECTED.set(0)
        # backoff
        backoff = self._influx_reconnect_backoff
        self._connect_influx()
        self._influx_reconnect_backoff = min(self._influx_reconnect_backoff * 2, self._influx_reconnect_backoff_max)
        await asyncio.sleep(backoff)
        return bool(self.influx_client and self.write_api)

    async def _maintenance_loop(self):
        """
        后台维护循环：
        - flush MQTT outbox（MQTT 断链期间缓存的推理结果）
        - flush Influx buffer（Influx 异常期间缓存的写入）
        - Influx 自修复（退避重连）
        """
        while True:
            try:
                if self._stop_event.is_set():
                    return

                await self._flush_mqtt_outbox()
                await self._flush_influx_buffer()
                await asyncio.sleep(self.flush_interval_sec)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("maintenance_loop_error", error=str(exc))
                await asyncio.sleep(self.flush_interval_sec)

    async def _flush_mqtt_outbox(self):
        if not self.enable_mqtt_outbox:
            return
        MQTT_OUTBOX_SIZE.set(len(self.mqtt_outbox))
        if not (self.client and self.connected):
            return
        # 尽力重放
        for _ in range(min(len(self.mqtt_outbox), 200)):
            try:
                topic, payload = self.mqtt_outbox[0]
            except Exception:
                break
            try:
                result = self.client.publish(topic, payload, qos=1)
                if result.rc != mqtt.MQTT_ERR_SUCCESS:
                    MQTT_PUBLISH_FAILURE_TOTAL.labels(str(result.rc)).inc()
                    break
                self.mqtt_outbox.popleft()
            except Exception as exc:
                MQTT_PUBLISH_FAILURE_TOTAL.labels(exc.__class__.__name__).inc()
                break
        MQTT_OUTBOX_SIZE.set(len(self.mqtt_outbox))

    async def _flush_influx_buffer(self):
        INFLUX_BUFFER_SIZE.set(len(self.influx_buffer))
        if not self.enable_influx_buffer:
            return
        if not self.influx_buffer:
            return
        if not await self._ensure_influx():
            return
        if not self.write_api:
            return

        # 批量写入（尽力）
        points = []
        for _ in range(min(len(self.influx_buffer), 500)):
            points.append(self.influx_buffer.popleft())
        try:
            self.write_api.write(
                bucket=self.influx_bucket,
                org=self.influx_org,
                record=points,
            )
        except Exception as exc:
            INFLUX_WRITE_FAILURE_TOTAL.labels(exc.__class__.__name__).inc()
            # 写失败：放回（保持顺序尽量）
            for pt in reversed(points):
                self._buffer_influx_point(pt)
        INFLUX_BUFFER_SIZE.set(len(self.influx_buffer))
    
    def _on_connect(self, client, userdata, flags, rc):
        """连接回调"""
        if rc == 0:
            logger.info("mqtt_connected")
            self.connected = True
            client.subscribe(self.realtime_topic)
        else:
            logger.error("mqtt_connection_failed", rc=rc)
            self.connected = False

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        logger.warning("mqtt_disconnected", rc=rc)
    
    def _on_message(self, client, userdata, msg):
        """消息回调"""
        try:
            payload = json.loads(msg.payload.decode())
            tag = payload.get("tag")
            value = payload.get("value")
            ts = payload.get("ts")
            ts_ns = payload.get("ts_ns")
            
            if tag and value is not None:
                MQTT_RX_TOTAL.inc()
                now = time.time()
                parsed_value = self._safe_float(value)
                if parsed_value is None or (isinstance(parsed_value, float) and (math.isnan(parsed_value) or math.isinf(parsed_value))):
                    MQTT_RX_INVALID_VALUE_TOTAL.inc()
                    return

                msg_ts_sec = now
                # edge-daq 同时带 ts（秒）与 ts_ns（纳秒）；优先 ts_ns
                if ts_ns is not None:
                    try:
                        msg_ts_sec = float(int(ts_ns)) / 1_000_000_000
                    except Exception:
                        msg_ts_sec = now
                elif ts is not None:
                    try:
                        msg_ts_sec = float(ts)
                    except Exception:
                        msg_ts_sec = now

                with self._lock:
                    self.data_cache[str(tag)] = float(parsed_value)
                    self._tag_ts_sec[str(tag)] = float(msg_ts_sec)

                    # freeze 检测：只有当值变化才更新 last_change_ts
                    prev = self._last_value.get(str(tag))
                    if prev is None or prev != float(parsed_value):
                        self._last_value[str(tag)] = float(parsed_value)
                        self._last_change_ts[str(tag)] = now

                    # 推理节流（按“收到完整特征”触发，但有最小间隔）
                    if self.inference_min_interval_sec > 0 and (now - self._last_inference_ts) < self.inference_min_interval_sec:
                        INFERENCE_SKIPPED_THROTTLE_TOTAL.inc()
                        return

                    if not self._has_all_features_locked():
                        return

                    snapshot, max_age = self._snapshot_features_locked(now)
                    if snapshot is None:
                        return

                    # staleness gate：避免用过期特征推理
                    if self.max_feature_age_sec > 0 and max_age > self.max_feature_age_sec:
                        # 记录哪些特征过期（只做 best-effort，不要太重）
                        for feature in self.model.input_features:
                            age = now - float(self._tag_ts_sec.get(feature, now))
                            if age > self.max_feature_age_sec:
                                FEATURE_STALE_TOTAL.labels(feature).inc()
                        FEATURE_MAX_AGE_SEC.set(float(max_age))
                        return

                    # frozen gate：特征长期不变可能是传感器冻结/链路卡住
                    if self.freeze_sec > 0:
                        frozen = []
                        for feature in self.model.input_features:
                            last_change = float(self._last_change_ts.get(feature, now))
                            if now - last_change > self.freeze_sec:
                                frozen.append(feature)
                        for f in frozen:
                            FEATURE_FROZEN_TOTAL.labels(f).inc()
                        # 冻结特征过多时跳过推理（避免输出误导）
                        max_frozen_allowed = max(1, int(os.getenv("SOFTSENSOR_MAX_FROZEN_FEATURES", "2") or "2"))
                        self._last_frozen_features = list(frozen)
                        if len(frozen) > max_frozen_allowed:
                            return

                    # 在 MQTT 网络线程里不要做重计算/写库：投递到 asyncio loop
                    self._last_inference_ts = now
                    self._last_feature_max_age_sec = float(max_age)
                    FEATURE_MAX_AGE_SEC.set(float(max_age))
                    INFERENCE_SCHEDULED_TOTAL.inc()

                self._schedule_inference(snapshot)
        
        except Exception as e:
            MQTT_RX_PARSE_ERROR_TOTAL.inc()
            logger.error("message_processing_error", error=str(e))
    
    def _has_all_features(self) -> bool:
        """检查是否收集到所有特征"""
        input_features = self.model.input_features
        
        for feature in input_features:
            if feature not in self.data_cache:
                return False
        
        return True

    def _has_all_features_locked(self) -> bool:
        """
        在持锁状态下检查特征齐全，并对缺失项记指标。
        """
        input_features = self.model.input_features
        ok = True
        for feature in input_features:
            if feature not in self.data_cache:
                FEATURE_MISSING_TOTAL.labels(feature).inc()
                ok = False
        return ok

    def _snapshot_features_locked(self, now: float):
        """
        在持锁状态下构造特征快照，并返回最大 age（秒）。
        """
        if not self.model.input_features:
            return {}, 0.0
        snapshot: dict[str, float] = {}
        max_age = 0.0
        for feature in self.model.input_features:
            if feature not in self.data_cache:
                return None, 0.0
            snapshot[feature] = float(self.data_cache[feature])
            age = now - float(self._tag_ts_sec.get(feature, now))
            if age > max_age:
                max_age = age
        return snapshot, max_age

    def _schedule_inference(self, snapshot: dict):
        """
        从 MQTT 线程安全地投递推理任务到 asyncio loop。
        """
        loop = self._loop
        if not loop:
            # 没有 loop 时退化：直接执行（不推荐，但避免静默不推理）
            try:
                self._run_inference(snapshot)
            except Exception:
                return
            return
        loop.call_soon_threadsafe(lambda: asyncio.create_task(self._run_inference_async(snapshot)))
    
    def _run_inference(self, feature_snapshot: dict):
        """运行推理"""
        result = self.model.inference(feature_snapshot)
        
        if result:
            # 发布推理结果
            self._publish_result(result, feature_snapshot)

    async def _run_inference_async(self, feature_snapshot: dict):
        if self._inference_inflight:
            INFERENCE_INFLIGHT_SKIPPED_TOTAL.inc()
            return
        self._inference_inflight = True
        start = time.time()
        try:
            # 未来 ONNX 推理可能较重，这里可改为 to_thread
            result = self.model.inference(feature_snapshot)
            if not result:
                INFERENCE_FAILED_TOTAL.inc()
                return
            self._publish_result(result, feature_snapshot)
            INFERENCE_DURATION_MS.set((time.time() - start) * 1000.0)
            INFERENCE_LAST_TS.set(int(time.time()))
            # 成功推理后，更新 last inference（用于 readiness）
            self._last_inference_ts = time.time()
        except Exception as exc:
            INFERENCE_FAILED_TOTAL.inc()
            logger.error("inference_task_failed", error=str(exc))
        finally:
            self._inference_inflight = False
    
    def _publish_result(self, result: dict, feature_snapshot: dict):
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
            
        # 发布到 MQTT：断链时入 outbox，由后台 flush
        try:
            encoded = json.dumps(payload)
            if self.client and self.connected:
                res = self.client.publish(self.ai_topic, encoded, qos=1)
                if res.rc != mqtt.MQTT_ERR_SUCCESS:
                    MQTT_PUBLISH_FAILURE_TOTAL.labels(str(res.rc)).inc()
                    self._buffer_mqtt_outbox(self.ai_topic, encoded)
            else:
                self._buffer_mqtt_outbox(self.ai_topic, encoded)
        except Exception as exc:
            MQTT_PUBLISH_FAILURE_TOTAL.labels(exc.__class__.__name__).inc()
            try:
                self._buffer_mqtt_outbox(self.ai_topic, json.dumps(payload))
            except Exception:
                pass
        
        self._write_influx(payload, timestamp)
        self._write_training_measurement(payload, feature_snapshot, timestamp, result)
            
        logger.info(
            "inference_result_published",
            moisture=payload["value"],
            confidence=payload["confidence"],
            topic=self.ai_topic,
        )

    def _buffer_mqtt_outbox(self, topic: str, encoded_payload: str):
        if not self.enable_mqtt_outbox:
            return
        maxlen = self.mqtt_outbox.maxlen
        if maxlen is not None and len(self.mqtt_outbox) >= maxlen:
            MQTT_OUTBOX_DROPPED_TOTAL.inc()
        self.mqtt_outbox.append((topic, encoded_payload))
        MQTT_OUTBOX_SIZE.set(len(self.mqtt_outbox))
    
    def _write_influx(self, payload: dict, timestamp: float):
        if not self.write_api:
            INFLUX_CONNECTED.set(0)
            self._buffer_influx_point(self._build_influx_point(payload, timestamp))
            return
        
        try:
            point = self._build_influx_point(payload, timestamp)
            self.write_api.write(bucket=self.influx_bucket, org=self.influx_org, record=point)
        except Exception as exc:
            INFLUX_WRITE_FAILURE_TOTAL.labels(exc.__class__.__name__).inc()
            logger.error("softsensor_influx_write_failed", error=str(exc))
            self._buffer_influx_point(self._build_influx_point(payload, timestamp))

    def _build_influx_point(self, payload: dict, timestamp: float) -> Point:
        return (
            Point(self.measurement)
            .tag("tag", payload["tag"])
            .field("value", payload["value"])
            .field("unit", payload["unit"])
            .field("confidence", payload["confidence"])
            .time(int(timestamp * 1e9))
        )

    def _buffer_influx_point(self, point: Point):
        if not self.enable_influx_buffer:
            return
        maxlen = self.influx_buffer.maxlen
        if maxlen is not None and len(self.influx_buffer) >= maxlen:
            INFLUX_BUFFER_DROPPED_TOTAL.inc()
        self.influx_buffer.append(point)
        INFLUX_BUFFER_SIZE.set(len(self.influx_buffer))

    def dq_snapshot(self) -> dict:
        """
        数据质量摘要（用于 /health 展示）。
        """
        now = time.time()
        last_inf = float(self._last_inference_ts or 0.0)
        return {
            "mqtt_connected": bool(self.connected),
            "influx_connected": bool(self.influx_client and self.write_api),
            "last_inference_ts": int(last_inf) if last_inf else None,
            "inference_age_sec": (now - last_inf) if last_inf else None,
            "feature_max_age_sec": float(self._last_feature_max_age_sec or 0.0),
            "frozen_features_count": len(self._last_frozen_features or []),
            "frozen_features": list(self._last_frozen_features or [])[:50],
            "mqtt_outbox_size": len(self.mqtt_outbox) if self.enable_mqtt_outbox else None,
            "influx_buffer_size": len(self.influx_buffer) if self.enable_influx_buffer else None,
        }

    def is_dq_ready(self) -> bool:
        """
        readiness 用的数据质量判定（用于告警链路）。
        """
        now = time.time()
        if self.ready_require_recent_inference:
            last_inf = float(self._last_inference_ts or 0.0)
            if not last_inf:
                return False
            if self.ready_max_inference_age_sec > 0 and (now - last_inf) > self.ready_max_inference_age_sec:
                return False
        if self.ready_max_feature_age_sec > 0 and float(self._last_feature_max_age_sec or 0.0) > self.ready_max_feature_age_sec:
            return False
        if self.ready_max_frozen_features >= 0 and len(self._last_frozen_features or []) > self.ready_max_frozen_features:
            return False
        return True

    def _write_training_measurement(self, payload: dict, feature_snapshot: dict, timestamp: float, result: dict):
        """将推理输入、输出写入训练数据 measurement"""
        if not self.write_api or not self.training_measurement:
            # training measurement 也会跟随主写入缓冲（简单合并）
            self._buffer_influx_point(self._build_training_point(payload, feature_snapshot, timestamp, result))
            return
        
        try:
            point = self._build_training_point(payload, feature_snapshot, timestamp, result)
            self.write_api.write(bucket=self.influx_bucket, org=self.influx_org, record=point)
        except Exception as exc:
            INFLUX_WRITE_FAILURE_TOTAL.labels(exc.__class__.__name__).inc()
            logger.error("softsensor_training_influx_write_failed", error=str(exc))
            self._buffer_influx_point(self._build_training_point(payload, feature_snapshot, timestamp, result))

    def _build_training_point(self, payload: dict, feature_snapshot: dict, timestamp: float, result: dict) -> Point:
        point = (
            Point(self.training_measurement)
            .tag("line_id", self.line_id)
            .tag("model_version", self.model_version)
            .tag("inference_source", self.inference_source)
        )
        batch_id = self._extract_batch_id()
        if batch_id:
            point.tag("batch_id", batch_id)
        point.field("moisture_predicted", payload["value"])
        point.field("moisture_confidence", payload["confidence"])
        inference_time = result.get("inference_time_ms")
        safe_inference_time = self._safe_float(inference_time)
        if safe_inference_time is not None:
            point.field("inference_time_ms", safe_inference_time)
        for feature_name, feature_value in feature_snapshot.items():
            safe_value = self._safe_float(feature_value)
            if safe_value is not None:
                point.field(feature_name, safe_value)
        point.time(int(timestamp * 1e9))
        return point

    def _snapshot_features(self):
        """当前特征快照，确保写入 training measurement 时一致"""
        if not self.model.input_features:
            return {}
        
        snapshot = {}
        for feature in self.model.input_features:
            if feature not in self.data_cache:
                return None
            snapshot[feature] = self.data_cache[feature]
        return snapshot

    @staticmethod
    def _safe_float(value):
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _extract_batch_id(self):
        """从缓存数据中提取批次编号（若存在）"""
        for key in self.batch_id_keys:
            value = self.data_cache.get(key)
            if value not in (None, "", "null", "None"):
                return str(value)
        return None
