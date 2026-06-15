"""
控制指令订阅与下发

通过 MQTT 订阅控制指令（Tag + Value），校验配置后写入 PLC。
"""

import json
import os
import time
import asyncio
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
        self.require_signature = os.getenv("CONTROL_REQUIRE_SIGNATURE", "true").lower() == "true"
        self.signature_secret = os.getenv("CONTROL_SIGNATURE_SECRET", "")
        cache_size = int(os.getenv("CONTROL_ID_CACHE_SIZE", "1000") or "1000")
        self._id_cache = deque(maxlen=cache_size)
        self._id_cache_set = set()
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
            except Exception:
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
        except Exception as e:
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
        except Exception as e:
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
            except Exception as e:
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
            tag_name = payload.get("tag")
            value = payload.get("value")
            msg_id = payload.get("id")
            msg_ts = payload.get("ts")
            msg_sig = payload.get("sig")
        except Exception as e:
            logger.error("control_payload_parse_error", error=str(e))
            return self._reject(None, "", None, "invalid_payload", {"error": str(e)}, remember=False)

        if not tag_name:
            logger.error("control_missing_tag")
            return self._reject(msg_id, "", value, "missing_tag")
        if self.require_id and not msg_id:
            CONTROL_REPLAY_DROPPED.inc()
            logger.warning("control_missing_id")
            return self._reject(msg_id, tag_name, value, "missing_id", remember=False)
        if self.require_timestamp and msg_ts is None:
            CONTROL_TS_INVALID.inc()
            logger.warning("control_missing_ts")
            return self._reject(msg_id, tag_name, value, "missing_ts")
        if msg_ts is not None:
            try:
                msg_ts_f = float(msg_ts)
            except Exception:
                CONTROL_TS_INVALID.inc()
                logger.warning("control_invalid_ts", ts=msg_ts)
                return self._reject(msg_id, tag_name, value, "invalid_ts", {"ts": msg_ts})
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
                )
        else:
            msg_ts_f = None

        if msg_id and self._is_duplicate(msg_id):
            CONTROL_REPLAY_DROPPED.inc()
            logger.warning("control_replay_detected", id=msg_id)
            return self._reject(msg_id, tag_name, value, "replay_detected", remember=False)
        if self.require_signature:
            if not self.signature_secret:
                logger.error("control_signature_secret_missing")
                CONTROL_SIG_INVALID.inc()
                return self._reject(msg_id, tag_name, value, "signature_secret_missing")
            if not msg_sig:
                CONTROL_SIG_INVALID.inc()
                logger.warning("control_signature_missing")
                return self._reject(msg_id, tag_name, value, "signature_missing")
            if not self._verify_signature(msg_sig, msg_id, msg_ts_f, tag_name, value):
                CONTROL_SIG_INVALID.inc()
                logger.warning("control_signature_invalid", id=msg_id)
                return self._reject(msg_id, tag_name, value, "signature_invalid")

        tag_config = self.tag_index.get(tag_name)
        if not tag_config:
            logger.error("control_tag_not_found", tag=tag_name)
            return self._reject(msg_id, tag_name, value, "tag_not_found")
        if self.allowed_tags and tag_name not in self.allowed_tags:
            logger.warning("control_tag_not_in_acl", tag=tag_name)
            return self._reject(msg_id, tag_name, value, "tag_not_in_acl")

        if not tag_config.get("control_enabled"):
            logger.warning("control_not_enabled", tag=tag_name)
            return self._reject(msg_id, tag_name, value, "control_not_enabled")

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
        scale = float(tag_config.get("scale", 1.0) or 1.0)
        if address is None:
            logger.error("control_missing_address", tag=tag_name)
            return self._reject(msg_id, tag_name, value, "missing_address")

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
                self._ack(msg_id, tag_name, "error", value, reason="plc_write_error", detail={"error": str(e)})

        # 注意：paho-mqtt 的 on_message 回调运行在其网络线程中。
        # 在真实运行时该线程通常没有 asyncio 事件循环；直接 get_event_loop() 可能抛异常导致控制失效。
        # - 如果当前线程就是 asyncio 主循环线程：直接 create_task
        # - 否则：用 run_coroutine_threadsafe 安全投递到 start() 捕获的主循环
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_do_write())
        except RuntimeError:
            if self._loop:
                fut = asyncio.run_coroutine_threadsafe(_do_write(), self._loop)
                # 不阻塞回调线程；记录可能的异常（避免静默失败）
                def _log_future_result(f):
                    try:
                        exc = f.exception()
                        if exc:
                            logger.error("control_write_task_failed", error=str(exc))
                    except Exception:
                        return

                fut.add_done_callback(_log_future_result)
            else:
                # 最后兜底：没有可用 loop 时直接同步运行（避免静默丢控制）
                asyncio.run(_do_write())
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
        if remember and msg_id and not self._is_duplicate(str(msg_id)):
            self._remember_id(str(msg_id))

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
        except Exception as e:
            self.last_ack_publish_ts = int(time.time())
            self.last_ack_publish_ok = False
            self.last_ack_error = str(e)
            self.last_ack_status = status
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
