"""
自愈审计与事件上报模块。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

import structlog

from monitor.self_heal import SelfHealOutcome


class SelfHealAuditor:
    """
    将自愈动作输出到日志、事件存储与 MQTT 主题。
    """

    def __init__(
        self,
        *,
        line_id: str,
        event_recorder: Optional[Any],
        mqtt_client_getter: Optional[Callable[[], Any]],
        topic: Optional[str],
    ) -> None:
        self.line_id = line_id
        self.event_recorder = event_recorder
        self._mqtt_client_getter = mqtt_client_getter or (lambda: None)
        self.topic = topic
        self.logger = structlog.get_logger(__name__)

    def emit(self, service_name: str, outcome: SelfHealOutcome) -> None:
        """记录自愈结果并输出到外部通道"""
        self._log(service_name, outcome)
        self._record_influx(service_name, outcome)
        self._publish_mqtt(service_name, outcome)

    def _log(self, service_name: str, outcome: SelfHealOutcome) -> None:
        execution_error = outcome.execution.error if outcome.execution else None
        self.logger.info(
            "self_heal_outcome",
            line=self.line_id,
            service=service_name,
            action=outcome.decision.action,
            reason=outcome.decision.reason,
            executed=outcome.executed,
            success=outcome.success,
            details=outcome.decision.details,
            state_updates=outcome.state_updates,
            error=execution_error,
        )

    def _record_influx(self, service_name: str, outcome: SelfHealOutcome) -> None:
        if not self.event_recorder:
            return

        execution_error = outcome.execution.error if outcome.execution else None
        try:
            self.event_recorder.record_event(
                "self_heal_action",
                tags={
                    "component": "edge-monitor",
                    "service": service_name,
                    "action": outcome.decision.action,
                    "reason": outcome.decision.reason,
                },
                fields={
                    "success": 1 if outcome.success else 0,
                    "executed": 1 if outcome.executed else 0,
                    "error": execution_error or "",
                },
            )
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - 故意宽捕：自愈审计是 best-effort sink，不能影响自愈动作
            self.logger.warning("self_heal_event_record_failed", error=str(exc))

    def _publish_mqtt(self, service_name: str, outcome: SelfHealOutcome) -> None:
        if not self.topic:
            return

        client = self._mqtt_client_getter()
        if not client:
            return

        payload = {
            "schema": "edge.monitor.self_heal.v1",
            "line": self.line_id,
            "service": service_name,
            "timestamp": outcome.timestamp,
            "decision": {
                "action": outcome.decision.action,
                "reason": outcome.decision.reason,
                "details": outcome.decision.details,
            },
            "executed": outcome.executed,
            "success": outcome.success,
            "state_updates": outcome.state_updates,
        }

        if outcome.execution:
            payload["execution"] = {
                "success": outcome.execution.success,
                "error": outcome.execution.error,
                "detail": outcome.execution.detail,
            }

        try:
            client.publish(self.topic, json.dumps(payload), qos=1, retain=False)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - 故意宽捕：自愈 MQTT 事件发布失败不能影响自愈动作
            self.logger.warning("self_heal_event_publish_failed", error=str(exc))
