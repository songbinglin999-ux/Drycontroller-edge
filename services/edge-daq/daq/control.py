"""
控制指令订阅与下发

通过 MQTT 订阅控制指令（Tag + Value），校验配置后写入 PLC。
"""

import json
import os
import time
from typing import Any, Dict, Optional
from collections import deque
import hmac
import hashlib

import paho.mqtt.client as mqtt
import structlog
from prometheus_client import Counter

logger = structlog.get_logger(__name__)
CONTROL_WRITE_SUCCESS = Counter("control_write_success_total", "Control writes success", ["type"])
CONTROL_WRITE_FAILURE = Counter("control_write_failure_total", "Control writes failure", ["type"])
CONTROL_RATE_LIMIT = Counter("control_rate_limited_total", "Control commands dropped by rate limit")
CONTROL_AUTH_MISSING = Counter("control_auth_missing_total", "Control auth missing (anonymous not allowed)")
CONTROL_REPLAY_DROPPED = Counter("control_replay_dropped_total", "Control commands dropped due to replay/idempotency")
CONTROL_TS_INVALID = Counter("control_timestamp_invalid_total", "Control commands dropped due to invalid timestamp")
CONTROL_ACK_SENT = Counter("control_ack_sent_total", "Control ack sent", ["status"])
CONTROL_SIG_INVALID = Counter("control_signature_invalid_total", "Control commands dropped due to signature invalid/missing")


def _build_tag_index(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    将 tags 配置拍平成 name -> config 的索引，便于快速查找。
    """
    index = {}
    tags_config = config.get("tags", {})
    for section, categories in tags_config.items():
        if not isinstance(categories, dict):
            continue
        for category, tags in categories.items():
            if not isinstance(tags, list):
                continue
            for tag in tags:
                name = tag.get("name")
                if name:
                    index[name] = tag
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
        self.idempotency_window_sec = float(os.getenv("CONTROL_IDEMPOTENCY_WINDOW_SEC", "30") or "30")
        self.require_timestamp = os.getenv("CONTROL_REQUIRE_TIMESTAMP", "true").lower() == "true"
        self.require_id = os.getenv("CONTROL_REQUIRE_ID", "true").lower() == "true"
        self.require_signature = os.getenv("CONTROL_REQUIRE_SIGNATURE", "false").lower() == "true"
        self.signature_secret = os.getenv("CONTROL_SIGNATURE_SECRET", "")
        cache_size = int(os.getenv("CONTROL_ID_CACHE_SIZE", "1000") or "1000")
        self._id_cache = deque(maxlen=cache_size)
        self._id_cache_set = set()

    async def start(self):
        """
        启动 MQTT 订阅
        """
        try:
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
            self.client.on_message = self._on_message
            self.client.connect(self.mqtt_broker, self.mqtt_port, keepalive=60)
            self.client.loop_start()
            logger.info(
                "control_subscriber_started",
                topic=self.topic,
                broker=self.mqtt_broker,
                port=self.mqtt_port,
            )
        except Exception as e:
            logger.error("control_subscriber_start_error", error=str(e))

    async def stop(self):
        """
        停止订阅
        """
        try:
            if self.client:
                self.client.loop_stop()
                self.client.disconnect()
            logger.info("control_subscriber_stopped")
        except Exception as e:
            logger.error("control_subscriber_stop_error", error=str(e))

    # -------------------- MQTT callbacks -------------------- #
    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("control_mqtt_connected")
            client.subscribe(self.topic, qos=1)
        else:
            logger.error("control_mqtt_connect_failed", return_code=rc)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            tag_name = payload.get("tag")
            value = payload.get("value")
            msg_id = payload.get("id")
            msg_ts = payload.get("ts")
            msg_sig = payload.get("sig")
        except Exception as e:
            logger.error("control_payload_parse_error", error=str(e))
            return

        if not tag_name:
            logger.error("control_missing_tag")
            return
        if self.require_id and not msg_id:
            CONTROL_REPLAY_DROPPED.inc()
            logger.warning("control_missing_id")
            return
        if self.require_timestamp and msg_ts is None:
            CONTROL_TS_INVALID.inc()
            logger.warning("control_missing_ts")
            return
        if msg_ts is not None:
            try:
                msg_ts_f = float(msg_ts)
            except Exception:
                CONTROL_TS_INVALID.inc()
                logger.warning("control_invalid_ts", ts=msg_ts)
                return
            now = time.time()
            if abs(now - msg_ts_f) > self.idempotency_window_sec:
                CONTROL_TS_INVALID.inc()
                logger.warning("control_ts_out_of_window", ts=msg_ts_f, window=self.idempotency_window_sec)
                return
        else:
            msg_ts_f = None

        if msg_id and self._is_duplicate(msg_id):
            CONTROL_REPLAY_DROPPED.inc()
            logger.warning("control_replay_detected", id=msg_id)
            return
        if self.require_signature:
            if not self.signature_secret:
                logger.error("control_signature_secret_missing")
                CONTROL_SIG_INVALID.inc()
                return
            if not msg_sig:
                CONTROL_SIG_INVALID.inc()
                logger.warning("control_signature_missing")
                return
            if not self._verify_signature(msg_sig, msg_id, msg_ts_f, tag_name, value):
                CONTROL_SIG_INVALID.inc()
                logger.warning("control_signature_invalid", id=msg_id)
                return

        tag_config = self.tag_index.get(tag_name)
        if not tag_config:
            logger.error("control_tag_not_found", tag=tag_name)
            return
        if self.allowed_tags and tag_name not in self.allowed_tags:
            logger.warning("control_tag_not_in_acl", tag=tag_name)
            return

        if not tag_config.get("control_enabled"):
            logger.warning("control_not_enabled", tag=tag_name)
            return

        # 频率限制：默认 500ms 同一 tag 至少间隔一次
        now = time.time()
        last_ts = self.last_sent_ts.get(tag_name, 0)
        if (now - last_ts) * 1000 < self.min_interval_ms:
            logger.warning("control_rate_limited", tag=tag_name, interval_ms=self.min_interval_ms)
            CONTROL_RATE_LIMIT.inc()
            return

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
                return

        # 根据类型写入
        tag_type = str(tag_config.get("type", "float")).lower()
        address = tag_config.get("address")
        byte_order = tag_config.get("byte_order", "big")
        scale = float(tag_config.get("scale", 1.0) or 1.0)
        if address is None:
            logger.error("control_missing_address", tag=tag_name)
            return

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
                    self._ack(msg_id, tag_name, "failed", value)
            except Exception as e:
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
                self._ack(msg_id, tag_name, "error", value)

        # 因为 on_message 在单独线程，异步调用需要用 loop
        import asyncio

        asyncio.get_event_loop().create_task(_do_write())
        self.last_sent_ts[tag_name] = now
        if msg_id:
            self._remember_id(msg_id)

    def _remember_id(self, msg_id: str):
        """将已处理的消息 ID 放入去重缓存。"""
        if len(self._id_cache) >= self._id_cache.maxlen:
            old = self._id_cache.popleft()
            self._id_cache_set.discard(old)
        self._id_cache.append(msg_id)
        self._id_cache_set.add(msg_id)

    def _is_duplicate(self, msg_id: str) -> bool:
        return msg_id in self._id_cache_set

    def _ack(self, msg_id: Optional[str], tag: str, status: str, value: Any):
        """向 ack 主题发送执行结果，便于上游确认/重试控制指令。"""
        if not msg_id:
            return
        try:
            payload = json.dumps(
                {
                    "id": msg_id,
                    "tag": tag,
                    "status": status,
                    "value": value,
                    "ts": int(time.time()),
                }
            )
            # 发布 QoS1，保证至少一次送达
            result = self.client.publish(self.ack_topic, payload, qos=1)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                CONTROL_ACK_SENT.labels(status).inc()
            else:
                logger.warning("control_ack_failed", id=msg_id, rc=result.rc, status=status)
        except Exception as e:
            logger.error("control_ack_error", id=msg_id, error=str(e), status=status)

    def _verify_signature(self, sig: str, msg_id: Optional[str], msg_ts: Optional[float], tag: str, value: Any) -> bool:
        """
        基于共享密钥的 HMAC-SHA256 校验，构造串：id:ts:tag:value
        """
        try:
            base = f"{msg_id}:{msg_ts}:{tag}:{value}"
            computed = hmac.new(self.signature_secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()
            return hmac.compare_digest(computed, str(sig))
        except Exception as e:
            logger.warning("control_signature_check_failed", error=str(e))
            return False


