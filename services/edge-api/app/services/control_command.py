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
from app.services.control_core.models import ALLOWED_CONTROL_SOFTPOINTS

logger = structlog.get_logger(__name__)


def _now_ts() -> float:
    # 使用 float 秒（与 edge-daq 校验逻辑一致）
    return float(time.time())


def _gen_id(prefix: str = "ctl") -> str:
    # 短且稳定：prefix + 16 hex
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _hmac_sig(secret: str, msg_id: str, msg_ts: float, tag: str, value: Any) -> str:
    """
    与 edge-daq 的校验严格对齐：base = f"{id}:{ts}:{tag}:{value}"
    注意：ts 必须是 float；value 的类型（int/float/bool）会影响字符串化结果。
    """
    base = f"{msg_id}:{float(msg_ts)}:{tag}:{value}"
    return hmac.new(secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()


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
        except Exception as exc:
            logger.error("control_ack_subscribe_failed", error=str(exc))

    def _on_ack_message(self, topic: str, payload: str) -> None:
        try:
            data = json.loads(payload)
        except Exception:
            logger.warning("control_ack_parse_failed", topic=topic, payload=payload)
            return

        msg_id = str(data.get("id") or "")
        if not msg_id:
            return

        fut = self._pending.pop(msg_id, None)
        if not fut:
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
        if loop:
            def _set_ack_result() -> None:
                if not fut.done():
                    fut.set_result(ack)

            loop.call_soon_threadsafe(_set_ack_result)

    def _validate_tag(self, tag: str, value: Any) -> Tuple[bool, str]:
        """
        控制安全校验（API侧先拦截，减少无效下发）：
        - V1 只允许 ALLOWED_CONTROL_SOFTPOINTS 中的 6 个频率软点
        - value 必须可转为数值并落在签收范围内
        """
        softpoint_range = ALLOWED_CONTROL_SOFTPOINTS.get(tag)
        if softpoint_range is None:
            return False, "softpoint_not_allowed"
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return False, "value_not_numeric"
        lo, hi, _unit = softpoint_range
        if numeric_value < lo or numeric_value > hi:
            return False, "value_out_of_range"
        return True, "ok"

    def _topic_for_line(self, line: str) -> str:
        return self._topic_tpl.format(line=line)

    def _ack_topic_for_line(self, line: str) -> str:
        return self._ack_tpl.format(line=line)

    async def send_set(
        self,
        line: str,
        tag: str,
        value: Any,
        *,
        msg_id: Optional[str] = None,
        wait_ack: bool = True,
        timeout_sec: float = 3.0,
    ) -> dict:
        """
        下发单条控制指令。默认等待 ACK（闭环）。
        """
        final_msg_id: Optional[str] = msg_id
        ack_status: str = "error"
        ack_at: Optional[dt.datetime] = None
        ack_detail: Dict[str, Any] = {}
        history_error: Optional[str] = None
        try:
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

            self._ensure_ack_subscription()
            self._loop = self._loop or asyncio.get_running_loop()

            final_msg_id = msg_id or _gen_id()
            msg_ts = _now_ts()
            sig = _hmac_sig(self._signature_secret, final_msg_id, msg_ts, tag, value)
            payload = {"id": final_msg_id, "ts": msg_ts, "tag": tag, "value": value, "sig": sig}
            topic = self._topic_for_line(line)

            # 注册 future（可选等待 ack）
            fut: Optional[asyncio.Future] = None
            if wait_ack:
                fut = self._loop.create_future()
                self._pending[final_msg_id] = fut

            # QoS1 发布
            try:
                mqtt_manager.publish(topic, payload, qos=1)
            except Exception as exc:
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
                        "value": value,
                        "wait_ack": wait_ack,
                        "timeout_sec": timeout_sec,
                    },
                    ack_status=ack_status,
                    ack_at=ack_at,
                    ack_detail=ack_detail,
                    error_msg=history_error,
                )
            except Exception:
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
            if not tag:
                results.append({"success": False, "error": "missing_tag"})
                continue
            if not wait_ack:
                resp = await self.send_set(line, tag, value, msg_id=msg_id, wait_ack=False)
                results.append(resp)
            else:
                tasks.append(self.send_set(line, tag, value, msg_id=msg_id, wait_ack=True, timeout_sec=timeout_sec))

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

        V1 只开放六个频率设定软点的普通写入；52 个 I/O 点保持只读。
        真实 /control/estop 必须等 EDGE-V2-P3 软点、authority、ACK/NAK
        和 fallback 模型签收后重新实现。
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
                "reason": "edge_v2_p3_softpoint_contract_pending",
                "required_contract": "EDGE-V2-P3 /control/estop",
            },
            "timestamp": int(time.time()),
        }


@lru_cache(maxsize=1)
def get_control_command_service() -> ControlCommandService:
    return ControlCommandService()
