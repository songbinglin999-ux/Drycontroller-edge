"""
控制指令订阅与下发

通过 MQTT 订阅控制指令（Tag + Value），校验配置后写入 PLC。
"""

import json
import math
import os
import time
import asyncio
import threading
from typing import Any, Dict, Optional
from collections import deque
import hmac
import hashlib

import paho.mqtt.client as mqtt
import structlog
from prometheus_client import Counter

from daq.frequency_transaction import (
    REQUEST_MODE_AUTO,
    REQUEST_MODE_MANUAL,
    SingleFrequencyTransactionWriter,
)

logger = structlog.get_logger(__name__)
CONTROL_WRITE_SUCCESS = Counter("control_write_success_total", "Control writes success", ["type"])
CONTROL_WRITE_FAILURE = Counter("control_write_failure_total", "Control writes failure", ["type"])
CONTROL_RATE_LIMIT = Counter("control_rate_limited_total", "Control commands dropped by rate limit")
CONTROL_AUTH_MISSING = Counter("control_auth_missing_total", "Control auth missing (anonymous not allowed)")
CONTROL_REPLAY_DROPPED = Counter("control_replay_dropped_total", "Control commands dropped due to replay/idempotency")
CONTROL_TS_INVALID = Counter("control_timestamp_invalid_total", "Control commands dropped due to invalid timestamp")
CONTROL_ACK_SENT = Counter("control_ack_sent_total", "Control ack sent", ["status"])
CONTROL_SIG_INVALID = Counter("control_signature_invalid_total", "Control commands dropped due to signature invalid/missing")

ALLOWED_CONTROL_SOFTPOINTS = {
    "VFM1_FREQ_SP",
    "VFM2_FREQ_SP",
    "VFM3_FREQ_SP",
    "VF1_FREQ_SP",
    "VF2_FREQ_SP",
    "VF3_FREQ_SP",
}
FREQUENCY_CHANNELS = {
    "VFM1_FREQ_SP": 1,
    "VFM2_FREQ_SP": 2,
    "VFM3_FREQ_SP": 3,
    "VF1_FREQ_SP": 4,
    "VF2_FREQ_SP": 5,
    "VF3_FREQ_SP": 6,
}
FREQUENCY_MIN_HZ = 0
FREQUENCY_MAX_HZ = 50
SINGLE_FREQUENCY_COMMAND_TYPE = "single_frequency_set"


def _coerce_transaction_mode(mode: Any) -> int:
    if isinstance(mode, bool):
        raise ValueError("mode must be 1 manual or 2 auto")
    value = int(mode)
    if value not in (REQUEST_MODE_MANUAL, REQUEST_MODE_AUTO):
        raise ValueError("mode must be 1 manual or 2 auto")
    return value


def _build_tag_index(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    控制写入只接受 modbus-address-map.yaml 中签收的 6 个频率软点。
    52 个 tags.yaml I/O 点均为只读，不再从 control_enabled 推导写点。
    """
    index = {}
    # 控制写软点的寄存器地址来自中间层 configs/modbus-address-map.yaml（位号→地址），
    # 不再从只读 tags.yaml 取。带 writable=true 且在白名单中的条目即控制软点；
    # range/unit 与 edge-api ALLOWED_CONTROL_SOFTPOINTS 镜像，edge-daq 独立做范围兜底
    # （纵深防御，见 P0-CTRL-NAK）。address/scale 在集成商签收前为 "TBD"。
    address_map = config.get("modbus_address_map", {})
    if isinstance(address_map, dict):
        for name, entry in address_map.items():
            if name not in ALLOWED_CONTROL_SOFTPOINTS:
                continue
            if not isinstance(entry, dict) or not entry.get("writable"):
                continue
            index[name] = {
                "name": name,
                "type": entry.get("type", "float"),
                "address": entry.get("address"),
                "scale": entry.get("scale", 1.0),
                "byte_order": entry.get("byte_order", "big"),
                "range": entry.get("range"),
                "unit": entry.get("unit", ""),
            }
    return index


class ControlSubscriber:
    """
    控制指令订阅器

    - 订阅控制指令 Topic
    - 校验标签是否允许控制
    - 校验范围
    - 写入 PLC（Modbus）
    """

    def __init__(self, modbus_client, config: Dict[str, Any], line_id: str):
        self.modbus_client = modbus_client
        self.config = config or {}
        self.line_id = line_id
        self.topic = os.getenv("MQTT_TOPIC_CONTROL", f"control/{line_id}/tags")
        self.client: Optional[mqtt.Client] = None
        self.tag_index = _build_tag_index(self.config)
        self.last_sent_ts: Dict[str, float] = {}
        self.min_interval_ms = int(os.getenv("CONTROL_MIN_INTERVAL_MS", "500"))

        self.mqtt_broker = os.getenv("MQTT_BROKER", "localhost")
        self.mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
        self.mqtt_user = os.getenv("MQTT_USER", "")
        self.mqtt_password = os.getenv("MQTT_PASSWORD", "")
        self.mqtt_tls = os.getenv("MQTT_TLS", "false").lower() == "true"
        self.mqtt_allow_insecure = os.getenv("MQTT_ALLOW_INSECURE", "false").lower() == "true"
        self.control_allow_anonymous = os.getenv("CONTROL_ALLOW_ANONYMOUS", "false").lower() == "true"
        allowed_tags_env = os.getenv("CONTROL_ALLOWED_TAGS", "")
        self.allowed_tags = {t.strip() for t in allowed_tags_env.split(",") if t.strip()} if allowed_tags_env else None
        self.ack_topic = os.getenv("CONTROL_ACK_TOPIC", f"control/{line_id}/acks")
        self.enable_d5100_writer = os.getenv("CONTROL_ENABLE_D5100_WRITER", "false").lower() == "true"
        self.frequency_writer = SingleFrequencyTransactionWriter(self.modbus_client)
        self._frequency_queue = deque()
        self._frequency_queue_draining = False
        self._frequency_queue_lock = threading.Lock()
        self.idempotency_window_sec = float(os.getenv("CONTROL_IDEMPOTENCY_WINDOW_SEC", "30") or "30")
        self.require_timestamp = os.getenv("CONTROL_REQUIRE_TIMESTAMP", "true").lower() == "true"
        self.require_id = os.getenv("CONTROL_REQUIRE_ID", "true").lower() == "true"
        self.require_signature = os.getenv("CONTROL_REQUIRE_SIGNATURE", "true").lower() == "true"
        self.signature_secret = os.getenv("CONTROL_SIGNATURE_SECRET", "")
        cache_size = int(os.getenv("CONTROL_ID_CACHE_SIZE", "1000") or "1000")
        self._id_cache = deque(maxlen=cache_size)
        self._id_cache_set = set()
        self._id_cache_lock = threading.Lock()
        # asyncio 主事件循环（用于从 MQTT 回调线程安全调度协程）
        self._loop = None
        self.started = False
        self.mqtt_connected = False
        self.subscribed = False
        self.subscribe_state = "not_started"
        self.start_error: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_connect_rc: Optional[int] = None
        self.last_disconnect_rc: Optional[int] = None
        self.last_subscribe_rc: Optional[int] = None
        self.last_ack_publish_ok: Optional[bool] = None
        self.last_ack_publish_ts: Optional[int] = None
        self.last_ack_publish_rc: Optional[int] = None
        self.last_ack_error: Optional[str] = None
        self.last_ack_status: Optional[str] = None

    async def start(self):
        """
        启动 MQTT 订阅
        """
        try:
            self.started = False
            self.start_error = None
            self.last_error = None
            self.subscribe_state = "starting"
            # 记录启动时的事件循环，便于从 paho 网络线程回调里调度写入
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None
            if not self.mqtt_tls and not self.mqtt_allow_insecure:
                raise RuntimeError("MQTT TLS is required. Set MQTT_TLS=true or explicitly allow insecure via MQTT_ALLOW_INSECURE=true.")
            if not (self.mqtt_user and self.mqtt_password) and not self.control_allow_anonymous:
                raise RuntimeError("MQTT auth required for control. Provide MQTT_USER/MQTT_PASSWORD or explicitly allow anonymous via CONTROL_ALLOW_ANONYMOUS=true.")
            if not (self.mqtt_user and self.mqtt_password):
                CONTROL_AUTH_MISSING.inc()
            self.client = mqtt.Client(client_id="edge-daq-control")
            if self.mqtt_user and self.mqtt_password:
                self.client.username_pw_set(self.mqtt_user, self.mqtt_password)
            if not self.mqtt_tls:
                logger.warning("mqtt_tls_disabled_for_control")
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message
            self.client.on_subscribe = self._on_subscribe
            self.client.connect(self.mqtt_broker, self.mqtt_port, keepalive=60)
            self.client.loop_start()
            self.started = True
            self.subscribe_state = "waiting_connect"
            logger.info(
                "control_subscriber_started",
                topic=self.topic,
                broker=self.mqtt_broker,
                port=self.mqtt_port,
            )
        except (OSError, RuntimeError, ValueError) as e:
            self.started = False
            self.mqtt_connected = False
            self.subscribed = False
            self.subscribe_state = "start_failed"
            self.start_error = str(e)
            self.last_error = str(e)
            logger.error("control_subscriber_start_error", error=str(e))

    async def stop(self):
        """
        停止订阅
        """
        try:
            if self.client:
                self.client.loop_stop()
                self.client.disconnect()
            self.started = False
            self.mqtt_connected = False
            self.subscribed = False
            self.subscribe_state = "stopped"
            logger.info("control_subscriber_stopped")
        except (OSError, RuntimeError, ValueError) as e:
            self.last_error = str(e)
            logger.error("control_subscriber_stop_error", error=str(e))

    def _ready_state(self) -> tuple[bool, Optional[str]]:
        if not self.started:
            return False, "start_failed" if self.start_error else "not_started"
        if not self.mqtt_connected:
            return False, "mqtt_disconnected"
        if not self.subscribed:
            return False, f"subscribe_{self.subscribe_state}"
        if self.last_ack_publish_ok is False:
            return False, "ack_publish_failed"
        return True, None

    def is_ready(self) -> bool:
        ready, _reason = self._ready_state()
        return ready

    def get_status(self) -> Dict[str, Any]:
        ready, reason = self._ready_state()
        return {
            "ready": ready,
            "reason": reason,
            "started": self.started,
            "start_error": self.start_error,
            "mqtt_connected": self.mqtt_connected,
            "subscribed": self.subscribed,
            "subscribe_state": self.subscribe_state,
            "topic": self.topic,
            "ack_topic": self.ack_topic,
            "broker": self.mqtt_broker,
            "port": self.mqtt_port,
            "last_error": self.last_error,
            "last_connect_rc": self.last_connect_rc,
            "last_disconnect_rc": self.last_disconnect_rc,
            "last_subscribe_rc": self.last_subscribe_rc,
            "ack_path": {
                "ok": self.last_ack_publish_ok,
                "last_publish_ts": self.last_ack_publish_ts,
                "last_publish_rc": self.last_ack_publish_rc,
                "last_error": self.last_ack_error,
                "last_status": self.last_ack_status,
            },
        }

    # -------------------- MQTT callbacks -------------------- #
    def _on_connect(self, client, userdata, flags, rc):
        self.last_connect_rc = rc
        if rc == 0:
            self.mqtt_connected = True
            self.subscribed = False
            self.subscribe_state = "subscribing"
            self.last_error = None
            logger.info("control_mqtt_connected")
            try:
                result = client.subscribe(self.topic, qos=1)
                subscribe_rc = result[0] if isinstance(result, tuple) else getattr(result, "rc", None)
                self.last_subscribe_rc = subscribe_rc
                if subscribe_rc == mqtt.MQTT_ERR_SUCCESS:
                    self.subscribe_state = "pending"
                else:
                    self.subscribe_state = "failed"
                    self.last_error = f"subscribe rc={subscribe_rc}"
                    logger.error("control_mqtt_subscribe_failed", topic=self.topic, rc=subscribe_rc)
            except (OSError, RuntimeError, ValueError) as e:
                self.subscribe_state = "failed"
                self.last_error = str(e)
                logger.error("control_mqtt_subscribe_error", topic=self.topic, error=str(e))
        else:
            self.mqtt_connected = False
            self.subscribed = False
            self.subscribe_state = "connect_failed"
            self.last_error = f"connect rc={rc}"
            logger.error("control_mqtt_connect_failed", return_code=rc)

    def _on_subscribe(self, client, userdata, mid, granted_qos, *args):
        failed = any(int(qos) == 128 for qos in granted_qos)
        if failed:
            self.subscribed = False
            self.subscribe_state = "failed"
            self.last_error = f"subscribe rejected qos={granted_qos}"
            logger.error("control_mqtt_subscribe_rejected", topic=self.topic, granted_qos=granted_qos)
            return
        self.subscribed = True
        self.subscribe_state = "subscribed"
        self.last_error = None
        logger.info("control_mqtt_subscribed", topic=self.topic, granted_qos=granted_qos)

    def _on_disconnect(self, client, userdata, rc, *args):
        self.last_disconnect_rc = rc
        self.mqtt_connected = False
        self.subscribed = False
        self.subscribe_state = "disconnected"
        if rc:
            self.last_error = f"disconnect rc={rc}"
            logger.warning("control_mqtt_disconnected", return_code=rc)
        else:
            logger.info("control_mqtt_disconnected", return_code=rc)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            command_type = str(payload.get("type") or "")
            tag_name = payload.get("tag")
            value = payload.get("value")
            msg_id = payload.get("id")
            msg_ts = payload.get("ts")
            msg_sig = payload.get("sig")
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.error("control_payload_parse_error", error=str(e))
            return self._reject(None, "", None, "invalid_payload", {"error": str(e)}, remember=False)

        if not tag_name:
            logger.error("control_missing_tag")
            return self._reject(msg_id, "", value, "missing_tag", remember=False)
        if self.require_id and not msg_id:
            CONTROL_REPLAY_DROPPED.inc()
            logger.warning("control_missing_id")
            return self._reject(msg_id, tag_name, value, "missing_id", remember=False)
        if self.require_timestamp and msg_ts is None:
            CONTROL_TS_INVALID.inc()
            logger.warning("control_missing_ts")
            return self._reject(msg_id, tag_name, value, "missing_ts", remember=False)
        if msg_ts is not None:
            try:
                msg_ts_f = float(msg_ts)
            except (TypeError, ValueError):
                CONTROL_TS_INVALID.inc()
                logger.warning("control_invalid_ts", ts=msg_ts)
                return self._reject(msg_id, tag_name, value, "invalid_ts", {"ts": msg_ts}, remember=False)
            now = time.time()
            if abs(now - msg_ts_f) > self.idempotency_window_sec:
                CONTROL_TS_INVALID.inc()
                logger.warning("control_ts_out_of_window", ts=msg_ts_f, window=self.idempotency_window_sec)
                return self._reject(
                    msg_id,
                    tag_name,
                    value,
                    "timestamp_out_of_window",
                    {"ts": msg_ts_f, "window_sec": self.idempotency_window_sec},
                    remember=False,
                )
        else:
            msg_ts_f = None

        if msg_id and self._is_duplicate(str(msg_id)):
            CONTROL_REPLAY_DROPPED.inc()
            logger.warning("control_replay_detected", id=msg_id)
            return self._reject(msg_id, tag_name, value, "replay_detected", remember=False)
        if self.require_signature:
            if not self.signature_secret:
                logger.error("control_signature_secret_missing")
                CONTROL_SIG_INVALID.inc()
                return self._reject(msg_id, tag_name, value, "signature_secret_missing", remember=False)
            if not msg_sig:
                CONTROL_SIG_INVALID.inc()
                logger.warning("control_signature_missing")
                return self._reject(msg_id, tag_name, value, "signature_missing", remember=False)
            if not self._verify_signature(
                msg_sig,
                msg_id,
                msg_ts_f,
                tag_name,
                value,
                command_type=command_type,
                mode=payload.get("mode"),
                channel=payload.get("channel"),
            ):
                CONTROL_SIG_INVALID.inc()
                logger.warning("control_signature_invalid", id=msg_id)
                return self._reject(msg_id, tag_name, value, "signature_invalid", remember=False)

        if command_type == SINGLE_FREQUENCY_COMMAND_TYPE:
            return self._handle_single_frequency_transaction(payload, msg_id, tag_name, value)
        if tag_name in FREQUENCY_CHANNELS:
            return self._reject(msg_id, tag_name, value, "single_frequency_transaction_required")

        tag_config = self.tag_index.get(tag_name)
        if not tag_config:
            logger.error("control_tag_not_found", tag=tag_name)
            return self._reject(msg_id, tag_name, value, "tag_not_found")
        if self.allowed_tags and tag_name not in self.allowed_tags:
            logger.warning("control_tag_not_in_acl", tag=tag_name)
            return self._reject(msg_id, tag_name, value, "tag_not_in_acl")

        # 频率限制：默认 500ms 同一 tag 至少间隔一次
        now = time.time()
        last_ts = self.last_sent_ts.get(tag_name, 0)
        if (now - last_ts) * 1000 < self.min_interval_ms:
            logger.warning("control_rate_limited", tag=tag_name, interval_ms=self.min_interval_ms)
            CONTROL_RATE_LIMIT.inc()
            return self._reject(
                msg_id,
                tag_name,
                value,
                "rate_limited",
                {"interval_ms": self.min_interval_ms},
                remember=False,
            )

        # 数值有效性校验：控制写入只接受有限数值。
        # 非数字（如字符串 "999"）会绕过下面 isinstance 门控的范围校验，
        # NaN 的比较恒为 False 同样穿透范围校验，二者都会把非法值直送 PLC。
        # bool 是 int 子类，需单独排除，避免被当 0/1 写入。
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            logger.warning("control_value_not_numeric", tag=tag_name, value=value)
            return self._reject(msg_id, tag_name, value, "value_not_numeric")

        # 范围校验
        value_range = tag_config.get("range")
        if value_range and isinstance(value, (int, float)):
            min_val, max_val = value_range
            if value < min_val or value > max_val:
                logger.warning(
                    "control_value_out_of_range",
                    tag=tag_name,
                    value=value,
                    range=value_range,
                )
                return self._reject(msg_id, tag_name, value, "value_out_of_range", {"range": value_range})

        # 根据类型写入
        tag_type = str(tag_config.get("type", "float")).lower()
        address = tag_config.get("address")
        byte_order = tag_config.get("byte_order", "big")
        if address is None:
            logger.error("control_missing_address", tag=tag_name)
            return self._reject(msg_id, tag_name, value, "missing_address")
        # Fail-closed：中间层地址/scale 在 PLC 集成商签收前为 "TBD"（非数值）。
        # 绝不能把 "TBD" 当寄存器地址/比例写下去，必须 NAK。bool 是 int 子类需单独排除。
        scale_raw = tag_config.get("scale", 1.0)
        if (
            not isinstance(address, int)
            or isinstance(address, bool)
            or not isinstance(scale_raw, (int, float))
            or isinstance(scale_raw, bool)
        ):
            logger.error(
                "control_address_not_configured", tag=tag_name, address=address, scale=scale_raw
            )
            return self._reject(msg_id, tag_name, value, "address_not_configured")
        scale = float(scale_raw) or 1.0

        # 工程值 -> 寄存器值转换
        reg_value = value
        if scale not in (0, 1) and isinstance(value, (int, float)):
            reg_value = value / scale

        async def _do_write():
            try:
                if tag_type == "float":
                    ok = await self.modbus_client.write_float(address, float(reg_value), byte_order=byte_order)
                    label = "float"
                elif tag_type in ("int32", "uint32"):
                    ok = await self.modbus_client.write_int32(address, int(reg_value), byte_order=byte_order)
                    label = "int32"
                else:
                    ok = await self.modbus_client.write_register(address, int(reg_value))
                    label = "int"

                if ok:
                    CONTROL_WRITE_SUCCESS.labels(label).inc()
                    logger.info(
                        "control_command_success",
                        tag=tag_name,
                        value=value,
                        address=address,
                        byte_order=byte_order,
                        scale=scale,
                    )
                    self._ack(msg_id, tag_name, "success", value)
                else:
                    CONTROL_WRITE_FAILURE.labels(label).inc()
                    logger.error(
                        "control_command_failed",
                        tag=tag_name,
                        value=value,
                        address=address,
                        byte_order=byte_order,
                        scale=scale,
                    )
                    self._ack(msg_id, tag_name, "failed", value, reason="plc_write_failed")
            except (OSError, RuntimeError, TypeError, ValueError) as e:
                CONTROL_WRITE_FAILURE.labels(tag_type).inc()
                logger.error(
                    "control_write_error",
                    tag=tag_name,
                    value=value,
                    address=address,
                    byte_order=byte_order,
                    scale=scale,
                    error=str(e),
                )
                self._ack(msg_id, tag_name, "error", value, reason="plc_write_error", detail={"error": str(e)})

        if msg_id and not self._remember_id_if_new(str(msg_id)):
            CONTROL_REPLAY_DROPPED.inc()
            logger.warning("control_replay_detected", id=msg_id)
            return self._reject(msg_id, tag_name, value, "replay_detected", remember=False)

        self._dispatch_write(_do_write)
        self.last_sent_ts[tag_name] = now

    def _handle_single_frequency_transaction(self, payload: dict, msg_id: Optional[str], tag_name: str, value: Any):
        if not self.enable_d5100_writer:
            return self._reject(msg_id, tag_name, value, "d5100_writer_disabled")
        channel = FREQUENCY_CHANNELS.get(tag_name)
        if channel is None:
            return self._reject(msg_id, tag_name, value, "softpoint_not_allowed")
        payload_channel = payload.get("channel")
        if payload_channel is None:
            return self._reject(msg_id, tag_name, value, "missing_channel")
        try:
            if isinstance(payload_channel, bool):
                raise ValueError("channel must be 1..6")
            payload_channel_i = int(payload_channel)
        except (TypeError, ValueError):
            return self._reject(
                msg_id,
                tag_name,
                value,
                "invalid_channel",
                {"channel": payload_channel},
            )
        if payload_channel_i < 1 or payload_channel_i > 6:
            return self._reject(
                msg_id,
                tag_name,
                value,
                "invalid_channel",
                {"channel": payload_channel_i},
            )
        if payload_channel_i != channel:
            return self._reject(
                msg_id,
                tag_name,
                value,
                "channel_tag_mismatch",
                {"expected_channel": channel, "actual_channel": payload_channel_i},
            )
        if payload.get("mode") is None:
            return self._reject(msg_id, tag_name, value, "missing_mode")
        try:
            mode = _coerce_transaction_mode(payload.get("mode"))
        except (TypeError, ValueError) as exc:
            return self._reject(
                msg_id,
                tag_name,
                value,
                "invalid_mode",
                {"error": str(exc)},
            )
        if self.allowed_tags and tag_name not in self.allowed_tags:
            logger.warning("control_tag_not_in_acl", tag=tag_name)
            return self._reject(msg_id, tag_name, value, "tag_not_in_acl")

        now = time.time()
        last_ts = self.last_sent_ts.get(tag_name, 0)
        if (now - last_ts) * 1000 < self.min_interval_ms:
            CONTROL_RATE_LIMIT.inc()
            logger.warning("control_rate_limited", tag=tag_name, interval_ms=self.min_interval_ms)
            return self._reject(
                msg_id,
                tag_name,
                value,
                "rate_limited",
                {"interval_ms": self.min_interval_ms},
                remember=False,
            )

        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            logger.warning("control_value_not_numeric", tag=tag_name, value=value)
            return self._reject(msg_id, tag_name, value, "value_not_numeric")
        if value < FREQUENCY_MIN_HZ or value > FREQUENCY_MAX_HZ:
            return self._reject(
                msg_id,
                tag_name,
                value,
                "value_out_of_range",
                {"range": [FREQUENCY_MIN_HZ, FREQUENCY_MAX_HZ]},
            )
        if not float(value).is_integer():
            return self._reject(msg_id, tag_name, value, "value_not_integer")

        if msg_id and not self._remember_id_if_new(str(msg_id)):
            CONTROL_REPLAY_DROPPED.inc()
            logger.warning("control_replay_detected", id=msg_id)
            return self._reject(msg_id, tag_name, value, "replay_detected", remember=False)

        seq = payload.get("seq")
        try:
            timeout_sec = float(payload.get("timeout_sec", 3.0) or 3.0)
        except (TypeError, ValueError):
            return self._reject(msg_id, tag_name, value, "invalid_timeout")
        if not math.isfinite(timeout_sec) or timeout_sec <= 0:
            return self._reject(msg_id, tag_name, value, "invalid_timeout")

        async def _do_write():
            result = await self.frequency_writer.write_one(
                channel,
                int(value),
                mode=mode,
                seq=seq,
                timeout_sec=timeout_sec,
                refresh_heartbeat=False,
            )
            detail = {
                "seq": result.seq,
                "channel": result.channel,
                "value": result.value,
                "ack_seq": result.ack_seq,
                "nak_seq": result.nak_seq,
                "nak_code": result.nak_code,
                "active_targets": result.active_targets,
            }
            if result.detail:
                detail["detail"] = result.detail

            if result.ok:
                CONTROL_WRITE_SUCCESS.labels("d5100").inc()
                self._ack(msg_id, tag_name, "success", value, detail=detail)
                return

            CONTROL_WRITE_FAILURE.labels("d5100").inc()
            status = "nak" if result.status == "nak" else "failed"
            self._ack(
                msg_id,
                tag_name,
                status,
                value,
                reason=result.reason or result.status,
                detail=detail,
            )

        self._enqueue_frequency_write(_do_write)
        self.last_sent_ts[tag_name] = now

    def _enqueue_frequency_write(self, write_coro_factory):
        with self._frequency_queue_lock:
            self._frequency_queue.append(write_coro_factory)
            if self._frequency_queue_draining:
                return
            self._frequency_queue_draining = True
        self._dispatch_write(self._drain_frequency_queue)

    async def _drain_frequency_queue(self):
        while True:
            with self._frequency_queue_lock:
                if not self._frequency_queue:
                    self._frequency_queue_draining = False
                    return
                write_coro_factory = self._frequency_queue.popleft()
            await write_coro_factory()

    def _dispatch_write(self, write_coro_factory):
        """把写 PLC 协程投递到可用事件循环；captured loop 已关闭时走同步兜底。"""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(write_coro_factory())
            return
        except RuntimeError:
            pass

        loop = self._loop
        if loop and not loop.is_closed():
            coro = write_coro_factory()
            try:
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
            except RuntimeError:
                coro.close()
            else:
                # 不阻塞回调线程；记录可能的异常（避免静默失败）
                def _log_future_result(f):
                    try:
                        exc = f.exception()
                        if exc:
                            logger.error("control_write_task_failed", error=str(exc))
                    except (asyncio.CancelledError, RuntimeError):
                        return

                fut.add_done_callback(_log_future_result)
                return

        # 最后兜底：没有可用 loop 时直接同步运行（避免静默丢控制）
        asyncio.run(write_coro_factory())

    def _remember_id_if_new(self, msg_id: str) -> bool:
        """原子保留消息 ID；返回 False 表示重复消息。"""
        with self._id_cache_lock:
            if msg_id in self._id_cache_set:
                return False
            if len(self._id_cache) >= self._id_cache.maxlen:
                old = self._id_cache.popleft()
                self._id_cache_set.discard(old)
            self._id_cache.append(msg_id)
            self._id_cache_set.add(msg_id)
            return True

    def _is_duplicate(self, msg_id: str) -> bool:
        with self._id_cache_lock:
            return msg_id in self._id_cache_set

    def _reject(
        self,
        msg_id: Optional[str],
        tag: str,
        value: Any,
        reason: str,
        detail: Optional[Dict[str, Any]] = None,
        *,
        remember: bool = True,
    ):
        """发布拒绝 NAK；有 id 的拒绝也进入幂等缓存，避免同一命令之后被接受。"""
        self._ack(msg_id, tag, "nak", value, reason=reason, detail=detail)
        if remember and msg_id:
            self._remember_id_if_new(str(msg_id))

    def _ack(
        self,
        msg_id: Optional[str],
        tag: str,
        status: str,
        value: Any,
        reason: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ):
        """向 ack 主题发送执行结果，便于上游确认/重试控制指令。"""
        try:
            message = {
                "id": msg_id or "",
                "tag": tag,
                "status": status,
                "value": value,
                "ts": int(time.time()),
            }
            if reason:
                message["reason"] = reason
            if detail:
                message["detail"] = detail
            payload = json.dumps(
                message
            )
            # 发布 QoS1，保证至少一次送达
            result = self.client.publish(self.ack_topic, payload, qos=1)
            self.last_ack_publish_ts = int(time.time())
            self.last_ack_publish_rc = result.rc
            self.last_ack_status = status
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self.last_ack_publish_ok = True
                self.last_ack_error = None
                CONTROL_ACK_SENT.labels(status).inc()
            else:
                self.last_ack_publish_ok = False
                self.last_ack_error = f"publish rc={result.rc}"
                logger.warning("control_ack_failed", id=msg_id, rc=result.rc, status=status)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as e:
            self.last_ack_publish_ts = int(time.time())
            self.last_ack_publish_ok = False
            self.last_ack_error = str(e)
            self.last_ack_status = status
            logger.error("control_ack_error", id=msg_id, error=str(e), status=status)

    def _verify_signature(
        self,
        sig: str,
        msg_id: Optional[str],
        msg_ts: Optional[float],
        tag: str,
        value: Any,
        *,
        command_type: str = "",
        mode: Any = None,
        channel: Any = None,
    ) -> bool:
        """
        基于共享密钥的 HMAC-SHA256 校验，构造串：id:ts:tag:value
        """
        try:
            base = f"{msg_id}:{msg_ts}:{tag}:{value}"
            if command_type == SINGLE_FREQUENCY_COMMAND_TYPE:
                base = f"{base}:{command_type}:{mode}:{channel}"
            computed = hmac.new(self.signature_secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()
            return hmac.compare_digest(computed, str(sig))
        except (TypeError, ValueError) as e:
            logger.warning("control_signature_check_failed", error=str(e))
            return False
