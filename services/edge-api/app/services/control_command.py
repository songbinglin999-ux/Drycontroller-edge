"""
控制命令下发（edge-api -> MQTT -> edge-daq -> ACK -> edge-api）

实现目标：
- 仅供 control_orchestrator 内部调用，不作为公开直写 API
- 控制命令使用 QoS1 发布
- 控制命令带幂等 id + timestamp + HMAC-SHA256 签名（与 edge-daq 对齐）
- 可选等待 edge-daq 的 ACK（强闭环），超时可控
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Optional, Tuple

import structlog

from app.core.mqtt_client import mqtt_manager
from app.services.control_history import write_control_history
from app.services.control_core.models import ALLOWED_CONTROL_SOFTPOINTS, FREQUENCY_CONTROL_SOFTPOINTS

logger = structlog.get_logger(__name__)

FREQUENCY_CHANNELS = {
    "VFM1_FREQ_SP": 1,
    "VFM2_FREQ_SP": 2,
    "VFM3_FREQ_SP": 3,
    "VF3_FREQ_SP": 6,
}
SINGLE_FREQUENCY_COMMAND_TYPE = "single_frequency_set"


def _now_ts() -> float:
    # 使用 float 秒（与 edge-daq 校验逻辑一致）
    return float(time.time())


def _gen_id(prefix: str = "ctl") -> str:
    # 短且稳定：prefix + 16 hex
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _hmac_sig(
    secret: str,
    msg_id: str,
    msg_ts: float,
    tag: str,
    value: Any,
    *,
    command_type: str = "",
    mode: Any = None,
    channel: Any = None,
) -> str:
    """
    与 edge-daq 的校验严格对齐：base = f"{id}:{ts}:{tag}:{value}"
    注意：ts 必须是 float；value 的类型（int/float/bool）会影响字符串化结果。
    """
    base = f"{msg_id}:{float(msg_ts)}:{tag}:{value}"
    if command_type == SINGLE_FREQUENCY_COMMAND_TYPE:
        base = f"{base}:{command_type}:{mode}:{channel}"
    return hmac.new(secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()


def _coerce_mode(mode: int | str) -> int:
    if isinstance(mode, bool):
        raise ValueError("mode must be MANUAL/1 or AUTO/2")
    if isinstance(mode, str):
        normalized = mode.strip().upper()
        if normalized == "MANUAL":
            return 1
        if normalized == "AUTO":
            return 2
    value = _coerce_int_field(mode, "mode")
    if value not in (1, 2):
        raise ValueError("mode must be MANUAL/1 or AUTO/2")
    return value


def _coerce_int_field(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized or not normalized.isdecimal():
            raise ValueError(f"{field_name} must be an integer")
        return int(normalized)
    raise ValueError(f"{field_name} must be an integer")


@dataclass
class ControlAck:
    id: str
    tag: str
    status: str
    value: Any
    ts: Optional[float] = None
    reason: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None


class ControlCommandService:
    """
    控制命令发布与 ACK 等待服务。
    """

    def __init__(self) -> None:
        self._pending: Dict[str, asyncio.Future] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscribed = False

        # 与 edge-daq 默认 topic 对齐，可通过环境变量覆盖
        self._topic_tpl = os.getenv("MQTT_TOPIC_CONTROL_TPL", "control/{line}/tags")
        self._ack_tpl = os.getenv("CONTROL_ACK_TOPIC_TPL", "control/{line}/acks")

        # 与 edge-daq 的 CONTROL_SIGNATURE_SECRET 必须一致
        self._signature_secret = os.getenv("CONTROL_SIGNATURE_SECRET", "").strip()

        # 统一订阅：用通配符接所有产线的 ack
        self._ack_pattern = os.getenv("CONTROL_ACK_SUB_PATTERN", "control/+/acks")

    def _ensure_ack_subscription(self) -> None:
        if self._subscribed:
            return
        # 订阅 + 注册回调：paho 线程回调里通过 loop.call_soon_threadsafe 设置 future
        try:
            mqtt_manager.subscribe(self._ack_pattern, qos=1)
            mqtt_manager.register_callback(self._ack_pattern, self._on_ack_message)
            self._subscribed = True
            logger.info("control_ack_subscribed", pattern=self._ack_pattern)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.error("control_ack_subscribe_failed", error=str(exc))

    def _on_ack_message(self, topic: str, payload: str) -> None:
        try:
            data = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("control_ack_parse_failed", topic=topic, payload=payload)
            return

        msg_id = str(data.get("id") or "")
        if not msg_id:
            return

        ack = ControlAck(
            id=msg_id,
            tag=str(data.get("tag") or ""),
            status=str(data.get("status") or "unknown"),
            value=data.get("value"),
            ts=data.get("ts"),
            reason=data.get("reason") or data.get("error"),
            detail=data.get("detail") if isinstance(data.get("detail"), dict) else None,
        )

        loop = self._loop
        if not loop:
            return

        def _deliver_ack() -> None:
            fut = self._pending.pop(msg_id, None)
            if fut and not fut.done():
                fut.set_result(ack)

        loop.call_soon_threadsafe(_deliver_ack)

    def _validate_tag(self, tag: str, value: Any) -> Tuple[bool, str]:
        """
        控制安全校验（API侧先拦截，减少无效下发）：
        - 只允许 ALLOWED_CONTROL_SOFTPOINTS 中已签收的控制软点
        - value 必须可转为数值并落在签收范围内
        """
        softpoint_range = ALLOWED_CONTROL_SOFTPOINTS.get(tag)
        if softpoint_range is None:
            return False, "softpoint_not_allowed"
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, "value_not_numeric"
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return False, "value_not_numeric"
        lo, hi, _unit = softpoint_range
        if numeric_value < lo or numeric_value > hi:
            return False, "value_out_of_range"
        if not numeric_value.is_integer():
            return False, "value_not_integer"
        return True, "ok"

    def _topic_for_line(self, line: str) -> str:
        return self._topic_tpl.format(line=line)

    def _ack_topic_for_line(self, line: str) -> str:
        return self._ack_tpl.format(line=line)

    def transport_status(self) -> dict:
        return {
            "ready": bool(mqtt_manager.connected and self._signature_secret and os.getenv("HMI_PROXY_ENABLED", "false").lower() != "true"),
            "proxy_mode": os.getenv("HMI_PROXY_ENABLED", "false").lower() == "true",
            "mqtt_connected": bool(mqtt_manager.connected),
            "signature_configured": bool(self._signature_secret),
            "ack_pattern": self._ack_pattern,
        }

    async def send_set(
        self,
        line: str,
        tag: str,
        value: Any,
        *,
        msg_id: Optional[str] = None,
        wait_ack: bool = True,
        timeout_sec: float = 3.0,
        mode: int | str = 2,
    ) -> dict:
        """
        下发单路频率控制指令。默认等待 ACK（闭环）。
        """
        if os.getenv("HMI_PROXY_ENABLED", "false").lower() == "true":
            return {"success": False, "error": "legacy_control_disabled_in_proxy_mode"}
        final_msg_id: Optional[str] = msg_id
        request_mode: Optional[int] = None
        channel: Optional[int] = None
        ack_status: str = "error"
        ack_at: Optional[dt.datetime] = None
        ack_detail: Dict[str, Any] = {}
        history_error: Optional[str] = None
        try:
            try:
                request_mode = _coerce_mode(mode)
            except (TypeError, ValueError) as exc:
                history_error = "invalid_mode"
                return {"success": False, "error": history_error, "detail": str(exc)}

            if not mqtt_manager.connected:
                history_error = "mqtt_not_connected"
                return {"success": False, "error": history_error}

            ok, reason = self._validate_tag(tag, value)
            if not ok:
                history_error = reason
                return {"success": False, "error": reason}

            if not self._signature_secret:
                # 与 edge-daq 默认要求签名对齐：没有密钥就不要发（避免“发了必失败”）
                history_error = "control_signature_secret_missing"
                return {"success": False, "error": history_error}

            channel = FREQUENCY_CHANNELS.get(tag)
            command_type = SINGLE_FREQUENCY_COMMAND_TYPE if tag in FREQUENCY_CONTROL_SOFTPOINTS else ""
            if tag in FREQUENCY_CONTROL_SOFTPOINTS and channel is None:
                history_error = "softpoint_not_allowed"
                return {"success": False, "error": history_error}

            self._loop = self._loop or asyncio.get_running_loop()
            self._ensure_ack_subscription()

            final_msg_id = msg_id or _gen_id()
            msg_ts = _now_ts()
            sig = _hmac_sig(
                self._signature_secret,
                final_msg_id,
                msg_ts,
                tag,
                value,
                command_type=command_type,
                mode=request_mode,
                channel=channel,
            )
            payload = {
                "id": final_msg_id,
                "ts": msg_ts,
                "tag": tag,
                "value": value,
                "sig": sig,
            }
            if command_type == SINGLE_FREQUENCY_COMMAND_TYPE:
                payload.update({"type": command_type, "channel": channel, "mode": request_mode})
            topic = self._topic_for_line(line)

            # 注册 future（可选等待 ack）
            fut: Optional[asyncio.Future] = None
            if wait_ack:
                fut = self._loop.create_future()
                self._pending[final_msg_id] = fut

            # QoS1 发布
            try:
                mqtt_manager.publish(topic, payload, qos=1)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                if final_msg_id:
                    self._pending.pop(final_msg_id, None)
                history_error = "control_publish_failed"
                logger.error("control_publish_failed", topic=topic, error=str(exc))
                return {
                    "success": False,
                    "status": "error",
                    "error": history_error,
                    "detail": str(exc),
                    "data": {"id": final_msg_id, "line": line, "topic": topic},
                    "timestamp": int(time.time()),
                }

            if not wait_ack:
                ack_status = "pending"
                return {
                    "success": True,
                    "status": "accepted",
                    "data": {"id": final_msg_id, "line": line, "topic": topic, "ack_topic": self._ack_topic_for_line(line)},
                    "timestamp": int(time.time()),
                }

            try:
                ack: ControlAck = await asyncio.wait_for(fut, timeout=timeout_sec)  # type: ignore[arg-type]
                ack_status = ack.status or "unknown"
                ack_at = dt.datetime.now(dt.timezone.utc)
                ack_detail = {
                    "id": ack.id,
                    "tag": ack.tag,
                    "value": ack.value,
                    "ack_ts": ack.ts,
                    "reason": ack.reason,
                    "detail": ack.detail,
                }
                if ack_status.lower() not in {"success", "ok", "acked"}:
                    history_error = ack.reason or ack_status
                    return {
                        "success": False,
                        "status": "nak",
                        "error": "control_nak",
                        "data": {
                            "id": ack.id,
                            "tag": ack.tag,
                            "ack_status": ack.status,
                            "reason": ack.reason,
                            "detail": ack.detail,
                            "value": ack.value,
                            "ack_ts": ack.ts,
                            "line": line,
                            "topic": topic,
                        },
                        "timestamp": int(time.time()),
                    }
                return {
                    "success": True,
                    "status": "acked",
                    "data": {
                        "id": ack.id,
                        "tag": ack.tag,
                        "ack_status": ack.status,
                        "reason": ack.reason,
                        "detail": ack.detail,
                        "value": ack.value,
                        "ack_ts": ack.ts,
                        "line": line,
                        "topic": topic,
                    },
                    "timestamp": int(time.time()),
                }
            except asyncio.TimeoutError:
                ack_status = "timeout"
                history_error = "ack_timeout"
                return {
                    "success": False,
                    "status": "timeout",
                    "error": "ack_timeout",
                    "data": {"id": final_msg_id, "line": line, "topic": topic},
                    "timestamp": int(time.time()),
                }
            finally:
                if final_msg_id:
                    self._pending.pop(final_msg_id, None)
        finally:
            try:
                await write_control_history(
                    command_type="set",
                    tag=tag,
                    value=value,
                    line_id=line,
                    idempotency_key=final_msg_id,
                    request_payload={
                        "line": line,
                        "tag": tag,
                        "channel": channel,
                        "value": value,
                        "mode": request_mode if request_mode is not None else mode,
                        "wait_ack": wait_ack,
                        "timeout_sec": timeout_sec,
                    },
                    ack_status=ack_status,
                    ack_at=ack_at,
                    ack_detail=ack_detail,
                    error_msg=history_error,
                )
            except RuntimeError:
                # write_control_history 内部已 swallow；此处兜底防御 import 期异常等极端情况
                pass

    async def send_batch(
        self,
        line: str,
        items: list[dict],
        *,
        wait_ack: bool = True,
        timeout_sec: float = 5.0,
    ) -> dict:
        """
        批量下发。wait_ack=true 时等待每条的 ack（整体超时）。
        items: [{tag, value, id?}]
        """
        results = []
        tasks = []
        for item in items:
            tag = item.get("tag")
            value = item.get("value")
            msg_id = item.get("id")
            mode = item.get("mode", 2)
            if not tag:
                results.append({"success": False, "error": "missing_tag"})
                continue
            if not wait_ack:
                resp = await self.send_set(line, tag, value, msg_id=msg_id, mode=mode, wait_ack=False)
                results.append(resp)
            else:
                tasks.append(self.send_set(line, tag, value, msg_id=msg_id, mode=mode, wait_ack=True, timeout_sec=timeout_sec))

        if tasks:
            # 单条 send_set 已含 timeout，这里 gather 只做汇总
            done = await asyncio.gather(*tasks, return_exceptions=False)
            results.extend(done)

        all_ok = all(r.get("success") is True for r in results)
        return {
            "success": all_ok,
            "count": len(results),
            "data": results,
            "timestamp": int(time.time()),
        }

    async def emergency_stop(
        self,
        line: str,
        *,
        wait_ack: bool = True,
        timeout_sec: float = 6.0,
    ) -> dict:
        """
        软件急停暂不可用。

        软件急停仍未签收；频率事务和远程 M 软点都不能替代急停。
        真实 /control/estop 必须等软点、authority、ACK/NAK
        和真实 PLC e2e 签收后再实现。
        """
        logger.warning(
            "software_estop_unavailable_pending_softpoint_contract",
            line=line,
            wait_ack=wait_ack,
            timeout_sec=timeout_sec,
        )
        return {
            "success": False,
            "status": "unavailable",
            "error": "software_estop_unavailable_pending_softpoint_contract",
            "data": {
                "reason": "software_estop_softpoint_contract_pending",
                "required_contract": "/control/estop softpoint contract",
            },
            "timestamp": int(time.time()),
        }


@lru_cache(maxsize=1)
def get_control_command_service() -> ControlCommandService:
    return ControlCommandService()
