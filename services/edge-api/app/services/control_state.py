from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from app.services.audit import write_audit
from app.services.control_command import (
    FREQUENCY_CHANNELS,
    ControlCommandService,
    get_control_command_service,
)
from app.services.control_core import build_recommendations
from app.services.control_core.models import (
    ALLOWED_CONTROL_SOFTPOINTS,
    SCHEMA_VERSION,
    ControlRecommendation,
    ProcessSnapshot,
    SuggestedAction,
)
from app.services.control_core.safety import action_validation_error
from app.services.control_core.snapshot import build_control_gate_snapshot, build_process_snapshot
from app.services.realtime_data import RealtimeDataService, get_realtime_data_service

CONTROL_MODES = ("MANUAL", "AUTO")
AUTO_PAUSED = "AUTO_PAUSED"
AUTO_PAUSED_DISPLAY = "自动暂停（需重新授权）"


class ControlStateService:
    """Production control product-mode state for edge-api.

    PLC/edge-daq still own D5100 transaction safety. This service only decides
    whether edge-api may enqueue a new AUTO command.
    """

    def __init__(
        self,
        *,
        realtime_service: Optional[RealtimeDataService] = None,
        command_service: Optional[ControlCommandService] = None,
        cooldown_sec: float = 60.0,
        max_delta_hz: float = 5.0,
    ) -> None:
        self._realtime = realtime_service or get_realtime_data_service()
        self._command = command_service or get_control_command_service()
        self._cooldown_sec = float(cooldown_sec)
        self._max_delta_hz = float(max_delta_hz)
        self._lock = asyncio.Lock()
        self._mode = "MANUAL"
        self._pause_reason: Optional[str] = None
        self._requires_reauthorization = False
        self._last_transition = _transition_payload(
            from_mode=None,
            to_mode="MANUAL",
            reason="startup_fail_closed",
            actor="edge-api",
        )
        self._authority_snapshot: Optional[dict] = None
        self._last_command_result: Optional[dict] = None
        self._last_auto_action: Optional[dict] = None
        self._in_flight = False
        self._last_auto_by_softpoint: Dict[str, Tuple[float, float]] = {}
        self._status_refresh_tasks: Dict[str, asyncio.Task] = {}

    async def get_status(self, *, line: Optional[str] = None, lookback: str = "5m") -> dict:
        line_id = line or "L1"
        transport = self._transport_status()
        blocks = self._transport_blocks(transport)
        if self._mode == "AUTO" and blocks:
            await self._pause_auto(
                reason=blocks[0],
                actor="edge-api",
                detail={"blocked_by": blocks, "authority_snapshot": self._authority_snapshot},
            )
        elif self._mode == "AUTO":
            self._schedule_status_refresh(line_id=line_id, lookback=lookback)
        return self._status_payload(line_id=line_id, transport=transport)

    async def authorize_auto(
        self,
        *,
        line: Optional[str],
        actor: str,
        reason: Optional[str] = None,
        lookback: str = "5m",
    ) -> dict:
        line_id = line or "L1"
        snapshot, snapshot_error = await self._safe_control_gate_snapshot(line_id=line_id, lookback=lookback)
        transport = self._transport_status()
        blocks = self._gate_blocks(snapshot, transport) if snapshot else ["DATA_MISSING_OR_STALE"]
        if snapshot_error:
            blocks.append(snapshot_error)
        if snapshot:
            self._authority_snapshot = _authority_snapshot(snapshot)

        if blocks:
            if self._mode == "AUTO":
                await self._pause_auto(
                    reason=blocks[0],
                    actor=actor,
                    detail={"blocked_by": blocks, "authority_snapshot": self._authority_snapshot},
                )
            await self._audit(
                actor=actor,
                action="control.mode.auto_authorize",
                result="denied",
                detail={"blocked_by": blocks, "reason": reason, "authority_snapshot": self._authority_snapshot},
            )
            return {
                "success": False,
                "error": "auto_authorization_failed",
                "blocked_by": blocks,
                "data": self._status_payload(line_id=line_id, transport=transport),
            }

        async with self._lock:
            old_mode = self._mode
            self._mode = "AUTO"
            self._pause_reason = None
            self._requires_reauthorization = False
            self._last_transition = _transition_payload(
                from_mode=old_mode,
                to_mode="AUTO",
                reason=reason or "operator_authorized_auto",
                actor=actor,
            )
            self._last_auto_action = None
        await self._audit(
            actor=actor,
            action="control.mode.auto_authorize",
            result="success",
            detail={"reason": reason, "authority_snapshot": self._authority_snapshot},
        )
        return {
            "success": True,
            "status": "AUTO",
            "data": self._status_payload(line_id=line_id, transport=transport),
        }

    async def switch_manual(
        self,
        *,
        line: Optional[str],
        actor: str,
        reason: Optional[str] = None,
    ) -> dict:
        line_id = line or "L1"
        async with self._lock:
            old_mode = self._mode
            self._mode = "MANUAL"
            self._pause_reason = None
            self._requires_reauthorization = old_mode in {"AUTO", AUTO_PAUSED}
            self._last_transition = _transition_payload(
                from_mode=old_mode,
                to_mode="MANUAL",
                reason=reason or "operator_selected_manual",
                actor=actor,
            )
            self._last_auto_action = None
        await self._audit(
            actor=actor,
            action="control.mode.manual",
            result="success",
            detail={"reason": reason, "previous_mode": old_mode},
        )
        return {
            "success": True,
            "status": "MANUAL",
            "data": self._status_payload(line_id=line_id, transport=self._transport_status()),
        }

    async def note_manual_intervention(
        self,
        *,
        line: Optional[str],
        actor: str,
        target_softpoint: str,
        value: Any,
        reason: Optional[str] = None,
    ) -> dict:
        line_id = line or "L1"
        async with self._lock:
            old_mode = self._mode
            if old_mode in {"AUTO", AUTO_PAUSED}:
                self._mode = "MANUAL"
                self._pause_reason = None
                self._requires_reauthorization = True
                self._last_transition = _transition_payload(
                    from_mode=old_mode,
                    to_mode="MANUAL",
                    reason=reason or "manual_frequency_intervention",
                    actor=actor,
                )
                self._last_auto_action = None
        await self._audit(
            actor=actor,
            action="control.mode.manual_intervention",
            result="success",
            detail={
                "line_id": line_id,
                "target_softpoint": target_softpoint,
                "value": value,
                "reason": reason,
                "previous_mode": old_mode,
                "requires_reauthorization": old_mode in {"AUTO", AUTO_PAUSED},
            },
        )
        return {"previous_mode": old_mode, "current_mode": self._mode}

    async def record_command_result(self, *, result: dict, command_scope: str) -> None:
        async with self._lock:
            self._last_command_result = {
                "scope": command_scope,
                "result": result,
                "recorded_at": _utc_iso(),
            }

    async def dispatch_auto(
        self,
        *,
        line: Optional[str],
        actor: str,
        timeout_sec: float = 3.0,
        lookback: str = "5m",
    ) -> dict:
        line_id = line or "L1"
        async with self._lock:
            if self._mode != "AUTO":
                return {
                    "success": False,
                    "error": "auto_not_authorized",
                    "data": self._status_payload(line_id=line_id, transport=self._transport_status()),
                }
            if self._in_flight:
                return {
                    "success": False,
                    "error": "auto_dispatch_in_flight",
                    "data": self._status_payload(line_id=line_id, transport=self._transport_status()),
                }
            self._in_flight = True

        try:
            snapshot, snapshot_error = await self._safe_snapshot(line_id=line_id, lookback=lookback)
            transport = self._transport_status()
            blocks = self._gate_blocks(snapshot, transport) if snapshot else ["DATA_MISSING_OR_STALE"]
            if snapshot_error:
                blocks.append(snapshot_error)
            if snapshot:
                self._authority_snapshot = _authority_snapshot(snapshot)

            if blocks:
                await self._pause_auto(
                    reason=blocks[0],
                    actor=actor,
                    detail={"blocked_by": blocks, "authority_snapshot": self._authority_snapshot},
                )
                return {
                    "success": False,
                    "error": "auto_gate_failed",
                    "blocked_by": blocks,
                    "data": self._status_payload(line_id=line_id, transport=transport),
                }

            dispatch_snapshot = _with_control_mode(snapshot, "AUTO")
            recommendation, action = _select_auto_action(build_recommendations(dispatch_snapshot))
            if not recommendation or not action:
                return {
                    "success": True,
                    "status": "no_action",
                    "data": self._status_payload(line_id=line_id, transport=transport),
                }

            validation_error = action_validation_error(action)
            if validation_error:
                await self._pause_auto(
                    reason=validation_error,
                    actor=actor,
                    detail={"recommendation": recommendation.model_dump(mode="json"), "action": action.model_dump(mode="json")},
                )
                return {
                    "success": False,
                    "error": "auto_action_rejected",
                    "blocked_by": [validation_error],
                    "data": self._status_payload(line_id=line_id, transport=transport),
                }

            rate_block = self._rate_block(action)
            if rate_block:
                await self._pause_auto(
                    reason=rate_block,
                    actor=actor,
                    detail={"recommendation_id": recommendation.id, "action": action.model_dump(mode="json")},
                )
                return {
                    "success": False,
                    "error": "auto_rate_gate_failed",
                    "blocked_by": [rate_block],
                    "data": self._status_payload(line_id=line_id, transport=transport),
                }

            result = await self._command.send_set(
                line=line_id,
                tag=action.target_softpoint,
                value=action.suggested_value,
                mode=2,
                wait_ack=True,
                timeout_sec=timeout_sec,
            )
            await self.record_command_result(result=result, command_scope="auto")
            mismatch = _active_target_mismatch(result, action)
            if result.get("success") is not True or mismatch:
                reason = mismatch or result.get("error") or result.get("status") or "auto_command_failed"
                await self._pause_auto(
                    reason=str(reason),
                    actor=actor,
                    detail={
                        "recommendation": recommendation.model_dump(mode="json"),
                        "action": action.model_dump(mode="json"),
                        "command_result": result,
                    },
                )
                return {
                    "success": False,
                    "error": "auto_command_failed",
                    "blocked_by": [str(reason)],
                    "command_result": result,
                    "data": self._status_payload(line_id=line_id, transport=transport),
                }

            now = time.monotonic()
            self._last_auto_by_softpoint[str(action.target_softpoint)] = (now, float(action.suggested_value))
            self._last_auto_action = {
                "recommendation_id": recommendation.id,
                "action": action.model_dump(mode="json"),
                "command_result": result,
                "dispatched_at": _utc_iso(),
            }
            await self._audit(
                actor=actor,
                action="control.auto.dispatch",
                result="success",
                detail=self._last_auto_action,
            )
            return {
                "success": True,
                "status": "dispatched",
                "recommendation": recommendation.model_dump(mode="json"),
                "action": action.model_dump(mode="json"),
                "command_result": result,
                "data": self._status_payload(line_id=line_id, transport=transport),
            }
        except Exception as exc:  # noqa: BLE001 - AUTO must fail closed on ordinary dispatch faults.
            reason = f"AUTO_DISPATCH_EXCEPTION:{type(exc).__name__}"
            await self._pause_auto(
                reason=reason,
                actor=actor,
                detail={"error": str(exc)},
            )
            return {
                "success": False,
                "error": "auto_dispatch_exception",
                "blocked_by": [reason],
                "data": self._status_payload(line_id=line_id, transport=self._transport_status()),
            }
        finally:
            async with self._lock:
                self._in_flight = False

    async def _pause_auto(self, *, reason: str, actor: str, detail: Optional[dict] = None) -> None:
        async with self._lock:
            old_mode = self._mode
            self._mode = AUTO_PAUSED
            self._pause_reason = reason
            self._requires_reauthorization = True
            self._last_transition = _transition_payload(
                from_mode=old_mode,
                to_mode=AUTO_PAUSED,
                reason=reason,
                actor=actor,
            )
        await self._audit(
            actor=actor,
            action="control.auto.paused",
            result="success",
            detail={"reason": reason, **(detail or {})},
        )

    async def _safe_snapshot(self, *, line_id: str, lookback: str) -> Tuple[Optional[ProcessSnapshot], Optional[str]]:
        try:
            return (
                await build_process_snapshot(
                    line_id=line_id,
                    lookback=lookback,
                    realtime_service=self._realtime,
                ),
                None,
            )
        except Exception as exc:  # noqa: BLE001 - malformed realtime payloads must fail closed.
            return None, f"SNAPSHOT_ERROR:{type(exc).__name__}"

    async def _safe_control_gate_snapshot(self, *, line_id: str, lookback: str) -> Tuple[Optional[ProcessSnapshot], Optional[str]]:
        try:
            return (
                await build_control_gate_snapshot(
                    line_id=line_id,
                    lookback=lookback,
                    realtime_service=self._realtime,
                ),
                None,
            )
        except Exception as exc:  # noqa: BLE001 - malformed realtime payloads must fail closed.
            return None, f"SNAPSHOT_ERROR:{type(exc).__name__}"

    def _transport_status(self) -> dict:
        status_fn = getattr(self._command, "transport_status", None)
        if callable(status_fn):
            return status_fn()
        return {"ready": True, "mqtt_connected": True, "signature_configured": True}

    def _transport_blocks(self, transport: dict) -> List[str]:
        blocks: List[str] = []
        if not transport.get("mqtt_connected"):
            blocks.append("MQTT_NOT_CONNECTED")
        if not transport.get("signature_configured"):
            blocks.append("CONTROL_SIGNATURE_SECRET_MISSING")
        return blocks

    def _gate_blocks(self, snapshot: Optional[ProcessSnapshot], transport: dict) -> List[str]:
        blocks: List[str] = []
        if snapshot is None or snapshot.freshness.is_degraded():
            blocks.append("DATA_MISSING_OR_STALE")
        state = snapshot.control_state if snapshot else None
        if state is None or state.AUTHORITY_OWNER != "EDGE":
            blocks.append("AUTHORITY_OWNER_NOT_EDGE")
        if state is None or state.AUTHORITY_EPOCH is None or state.AUTHORITY_EPOCH <= 0:
            blocks.append("AUTHORITY_EPOCH_INVALID")
        if state is None or state.AUTH_TRANSITION_STATE != 0:
            blocks.append("AUTHORITY_TRANSITION_NOT_STABLE")
        if state is None or state.HMI_DIRECT_ENABLE is not False:
            blocks.append("HMI_DIRECT_ENABLE_NOT_FALSE")
        if state is not None and state.maintenance_lock is True:
            blocks.append("MAINTENANCE_LOCK_ACTIVE")
        if state is not None and state.emergency_stop is True:
            blocks.append("EMERGENCY_STOP_ACTIVE")
        blocks.extend(self._transport_blocks(transport))
        return list(dict.fromkeys(blocks))

    def _schedule_status_refresh(self, *, line_id: str, lookback: str) -> None:
        task = self._status_refresh_tasks.get(line_id)
        if task and not task.done():
            return
        task = asyncio.create_task(self._refresh_control_gate(line_id=line_id, lookback=lookback))
        self._status_refresh_tasks[line_id] = task
        task.add_done_callback(lambda done: self._finish_status_refresh(line_id, done))

    def _finish_status_refresh(self, line_id: str, task: asyncio.Task) -> None:
        self._status_refresh_tasks.pop(line_id, None)
        if not task.cancelled():
            task.exception()

    async def _refresh_control_gate(self, *, line_id: str, lookback: str) -> None:
        snapshot, snapshot_error = await self._safe_control_gate_snapshot(line_id=line_id, lookback=lookback)
        transport = self._transport_status()
        blocks = self._gate_blocks(snapshot, transport) if snapshot else ["DATA_MISSING_OR_STALE"]
        if snapshot_error:
            blocks.append(snapshot_error)
        if snapshot:
            self._authority_snapshot = _authority_snapshot(snapshot)
        if self._mode == "AUTO" and blocks:
            await self._pause_auto(
                reason=blocks[0],
                actor="edge-api",
                detail={"blocked_by": blocks, "authority_snapshot": self._authority_snapshot},
            )

    def _rate_block(self, action: SuggestedAction) -> Optional[str]:
        tag = str(action.target_softpoint)
        value = float(action.suggested_value)
        now = time.monotonic()
        last = self._last_auto_by_softpoint.get(tag)
        if last and now - last[0] < self._cooldown_sec:
            return "AUTO_COOLDOWN_ACTIVE"
        base_value = last[1] if last else action.current_value
        if base_value is not None and abs(value - float(base_value)) > self._max_delta_hz:
            return "AUTO_RATE_DELTA_EXCEEDED"
        return None

    def _status_payload(self, *, line_id: str, transport: dict) -> dict:
        return {
            "mode": self._mode,
            "line_id": line_id,
            "auto_control_enabled": self._mode == "AUTO",
            "operation_modes": list(CONTROL_MODES),
            "non_selectable_modes": [AUTO_PAUSED],
            "auto_pause_state": AUTO_PAUSED,
            "auto_paused_display": AUTO_PAUSED_DISPLAY,
            "auto_pause_is_operator_mode": False,
            "pause_reason": self._pause_reason,
            "requires_reauthorization": self._requires_reauthorization,
            "last_transition": self._last_transition,
            "authority_snapshot": self._authority_snapshot,
            "transport": transport,
            "last_command_result": self._last_command_result,
            "last_auto_action": self._last_auto_action,
            "auto_dispatch_status": self._dispatch_status(),
            "auto_in_flight": self._in_flight,
            "controllable_tags": sorted(ALLOWED_CONTROL_SOFTPOINTS),
            "control_softpoints_status": "frequency_softpoints_allowlist_only",
            "suggestion_schema": SCHEMA_VERSION,
            "requires_confirmation": False,
            "production_requires_confirmation": False,
            "production_authorization_model": "MANUAL_AUTO_AUTO_PAUSED",
            "manual_command_entrypoint": "/api/v1/control/manual/set",
            "auto_authorize_entrypoint": "/api/v1/control/mode",
            "auto_dispatch_entrypoint": "/api/v1/control/auto/dispatch",
            "cooldown_policy": {
                "per_softpoint_cooldown_sec": self._cooldown_sec,
                "max_delta_hz_per_command": self._max_delta_hz,
                "source": "conservative_default",
            },
        }

    def _dispatch_status(self) -> str:
        if self._in_flight:
            return "in_flight"
        if self._mode == "AUTO":
            return "ready"
        if self._mode == AUTO_PAUSED:
            return "paused_requires_reauthorization"
        return "manual_stopped"

    async def _audit(self, *, actor: str, action: str, result: str, detail: Optional[dict]) -> None:
        await write_audit(
            actor=actor,
            action=action,
            resource_type="control_state",
            resource_id="edge-api",
            result=result,
            detail=detail,
        )


def _select_auto_action(recommendations: List[ControlRecommendation]) -> Tuple[Optional[ControlRecommendation], Optional[SuggestedAction]]:
    for recommendation in recommendations:
        if not recommendation.actionable:
            continue
        for action in recommendation.suggested_actions:
            if (
                action.is_executable()
                and action.target_softpoint in ALLOWED_CONTROL_SOFTPOINTS
                and action.suggested_value is not None
            ):
                return recommendation, action
    return None, None


def _active_target_mismatch(result: dict, action: SuggestedAction) -> Optional[str]:
    data = result.get("data") if isinstance(result, dict) else None
    detail = data.get("detail") if isinstance(data, dict) else None
    active_targets = detail.get("active_targets") if isinstance(detail, dict) else None
    if not isinstance(active_targets, list):
        return None
    channel = FREQUENCY_CHANNELS.get(str(action.target_softpoint))
    if not channel or len(active_targets) < channel:
        return "active_targets_missing"
    try:
        actual = float(active_targets[channel - 1])
        expected = float(action.suggested_value)
    except (TypeError, ValueError):
        return "active_targets_invalid"
    if actual != expected:
        return "active_targets_mismatch"
    return None


def _authority_snapshot(snapshot: ProcessSnapshot) -> dict:
    state = snapshot.control_state
    return {
        "freshness": snapshot.freshness.model_dump(),
        "AUTHORITY_OWNER": state.AUTHORITY_OWNER,
        "AUTHORITY_EPOCH": state.AUTHORITY_EPOCH,
        "AUTH_TRANSITION_STATE": state.AUTH_TRANSITION_STATE,
        "HMI_DIRECT_ENABLE": state.HMI_DIRECT_ENABLE,
        "maintenance_lock": state.maintenance_lock,
        "emergency_stop": state.emergency_stop,
        "snapshot_at": snapshot.timestamp.isoformat(),
    }


def _transition_payload(*, from_mode: Optional[str], to_mode: str, reason: str, actor: str) -> dict:
    return {
        "from": from_mode,
        "to": to_mode,
        "reason": reason,
        "actor": actor,
        "at": _utc_iso(),
    }


def _with_control_mode(snapshot: ProcessSnapshot, mode: str) -> ProcessSnapshot:
    return snapshot.model_copy(
        update={
            "control_state": snapshot.control_state.model_copy(update={"CONTROL_MODE": mode}),
        }
    )


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@lru_cache(maxsize=1)
def get_control_state_service() -> ControlStateService:
    return ControlStateService()
