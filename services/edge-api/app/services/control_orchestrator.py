from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional

from app.services.audit import write_audit
from app.services.control_command import ControlCommandService, get_control_command_service
from app.services.control_core import ControlRecommendation, build_recommendations
from app.services.control_core.models import (
    ALLOWED_CONTROL_SOFTPOINTS,
    SCHEMA_VERSION,
    SuggestedAction,
    expires_from,
    recommendation_id,
    softpoint_feedback_tag,
    softpoint_unit,
    utc_now,
)
from app.services.control_core.safety import action_validation_error, apply_recommendation_safety_gates
from app.services.control_core.snapshot import build_process_snapshot
from app.services.realtime_data import RealtimeDataService, get_realtime_data_service


class ControlOrchestrator:
    """Generate recommendations and gate authorized frequency commands."""

    def __init__(
        self,
        realtime_service: Optional[RealtimeDataService] = None,
        command_service: Optional[ControlCommandService] = None,
    ) -> None:
        self._realtime = realtime_service or get_realtime_data_service()
        self._command = command_service or get_control_command_service()
        self._cache: Dict[str, ControlRecommendation] = {}
        self._dismissed: Dict[str, datetime] = {}

    async def get_recommendations(self, *, line: Optional[str], lookback: str = "5m") -> dict:
        line_id = line or "L1"
        snapshot = await build_process_snapshot(
            line_id=line_id,
            lookback=lookback,
            realtime_service=self._realtime,
        )
        recommendations = [
            item for item in build_recommendations(snapshot)
            if not self._is_dismissed(item)
        ]
        for item in recommendations:
            self._cache[item.id] = item
            await self._audit_recommendation(item)

        return {
            "success": True,
            "data": {
                "schema_version": SCHEMA_VERSION,
                "line_id": line_id,
                "freshness": snapshot.freshness.model_dump(),
                "control_state": snapshot.control_state.model_dump(),
                "recommendations": [item.model_dump(mode="json") for item in recommendations],
                "allowed_targets": sorted(ALLOWED_CONTROL_SOFTPOINTS),
                "requires_confirmation": True,
                "production_authorization_model": "MANUAL_AUTO_MODE",
                "auto_dispatch_status": "not_implemented",
                "timestamp": int(datetime.now(timezone.utc).timestamp()),
            },
        }

    async def confirm_recommendation(
        self,
        *,
        recommendation_id: str,
        line: Optional[str],
        actor: str,
        timeout_sec: float = 3.0,
    ) -> dict:
        recommendation = await self._find_recommendation(recommendation_id, line=line)
        if recommendation is None:
            await self._audit_confirmation(
                recommendation_id=recommendation_id,
                actor=actor,
                result="denied",
                detail={"error": "recommendation_not_found"},
            )
            return {"success": False, "error": "recommendation_not_found"}

        if _is_expired(recommendation):
            await self._audit_confirmation(
                recommendation_id=recommendation.id,
                actor=actor,
                result="denied",
                detail={"error": "recommendation_expired"},
            )
            return {"success": False, "error": "recommendation_expired"}

        if not recommendation.actionable:
            await self._audit_confirmation(
                recommendation_id=recommendation.id,
                actor=actor,
                result="denied",
                detail={
                    "error": "recommendation_not_actionable",
                    "blocked_by": recommendation.blocked_by,
                },
            )
            return {
                "success": False,
                "error": "recommendation_not_actionable",
                "blocked_by": recommendation.blocked_by,
                "recommendation": recommendation.model_dump(mode="json"),
            }

        command_results = []
        for action in _executable_actions(recommendation.suggested_actions):
            validation_error = action_validation_error(action)
            if validation_error:
                result = {"success": False, "error": validation_error, "action": action.model_dump(mode="json")}
            else:
                result = await self._command.send_set(
                    line=recommendation.line_id,
                    tag=action.target_softpoint,
                    value=action.suggested_value,
                    mode=2,
                    wait_ack=True,
                    timeout_sec=timeout_sec,
                )
            command_results.append(result)

        success = all(item.get("success") is True for item in command_results)
        await self._audit_confirmation(
            recommendation_id=recommendation.id,
            actor=actor,
            result="success" if success else "failed",
            detail={
                "recommendation": recommendation.model_dump(mode="json"),
                "command_results": command_results,
            },
        )
        return {
            "success": success,
            "status": "confirmed" if success else "failed",
            "recommendation": recommendation.model_dump(mode="json"),
            "command_results": command_results,
        }

    async def confirm_manual_action(
        self,
        *,
        line: Optional[str],
        target_softpoint: str,
        value: float,
        actor: str,
        operator_id: Optional[str] = None,
        request_id: Optional[str] = None,
        reason: Optional[str] = None,
        timeout_sec: float = 3.0,
    ) -> dict:
        recommendation = await self._build_manual_recommendation(
            line=line,
            target_softpoint=target_softpoint,
            value=value,
            operator_id=operator_id,
            request_id=request_id,
            reason=reason,
        )
        self._cache[recommendation.id] = recommendation

        if not recommendation.actionable:
            await self._audit_confirmation(
                recommendation_id=recommendation.id,
                actor=actor,
                result="denied",
                detail={
                    "error": "manual_command_not_actionable",
                    "blocked_by": recommendation.blocked_by,
                    "recommendation": recommendation.model_dump(mode="json"),
                },
            )
            return {
                "success": False,
                "error": "manual_command_not_actionable",
                "blocked_by": recommendation.blocked_by,
                "recommendation": recommendation.model_dump(mode="json"),
            }

        action = recommendation.suggested_actions[0]
        result = await self._command.send_set(
            line=recommendation.line_id,
            tag=action.target_softpoint,
            value=action.suggested_value,
            msg_id=request_id,
            mode=1,
            wait_ack=True,
            timeout_sec=timeout_sec,
        )
        success = result.get("success") is True
        await self._audit_confirmation(
            recommendation_id=recommendation.id,
            actor=actor,
            result="success" if success else "failed",
            detail={
                "recommendation": recommendation.model_dump(mode="json"),
                "command_results": [result],
            },
        )
        return {
            "success": success,
            "status": "confirmed" if success else "failed",
            "recommendation": recommendation.model_dump(mode="json"),
            "command_results": [result],
        }

    async def dismiss_recommendation(
        self,
        *,
        recommendation_id: str,
        actor: str,
        reason: Optional[str] = None,
    ) -> dict:
        expires_at = datetime.now(timezone.utc)
        cached = self._cache.get(recommendation_id)
        if cached:
            expires_at = cached.expires_at
        self._dismissed[recommendation_id] = expires_at
        await write_audit(
            actor=actor,
            action="control.recommendation.dismissed",
            resource_type="control_recommendation",
            resource_id=recommendation_id,
            result="success",
            detail={"reason": reason} if reason else None,
        )
        return {"success": True, "status": "dismissed", "id": recommendation_id}

    async def _find_recommendation(
        self,
        recommendation_id: str,
        line: Optional[str],
    ) -> Optional[ControlRecommendation]:
        cached = self._cache.get(recommendation_id)
        line_id = line or (cached.line_id if cached else None)
        payload = await self.get_recommendations(line=line_id, lookback="5m")
        for raw in payload["data"]["recommendations"]:
            if raw["id"] == recommendation_id:
                return self._cache.get(recommendation_id)
        return None

    def _is_dismissed(self, recommendation: ControlRecommendation) -> bool:
        expires_at = self._dismissed.get(recommendation.id)
        if not expires_at:
            return False
        if expires_at <= datetime.now(timezone.utc):
            self._dismissed.pop(recommendation.id, None)
            return False
        return True

    async def _audit_recommendation(self, recommendation: ControlRecommendation) -> None:
        await write_audit(
            actor="edge-api",
            action="control.recommendation.generated",
            resource_type="control_recommendation",
            resource_id=recommendation.id,
            result="success",
            detail=recommendation.model_dump(mode="json"),
        )

    async def _audit_confirmation(
        self,
        *,
        recommendation_id: str,
        actor: str,
        result: str,
        detail: Dict[str, Any],
    ) -> None:
        await write_audit(
            actor=actor,
            action="control.recommendation.confirmed",
            resource_type="control_recommendation",
            resource_id=recommendation_id,
            result=result,
            detail=detail,
        )

    async def _build_manual_recommendation(
        self,
        *,
        line: Optional[str],
        target_softpoint: str,
        value: float,
        operator_id: Optional[str],
        request_id: Optional[str],
        reason: Optional[str],
    ) -> ControlRecommendation:
        line_id = line or "L1"
        snapshot = await build_process_snapshot(
            line_id=line_id,
            lookback="5m",
            realtime_service=self._realtime,
        )
        now = utc_now()
        feedback_tag = softpoint_feedback_tag(target_softpoint)
        current_value = _as_float(snapshot.tags.get(feedback_tag)) if feedback_tag else None
        unit = softpoint_unit(target_softpoint) if target_softpoint in ALLOWED_CONTROL_SOFTPOINTS else None
        action = SuggestedAction(
            target_softpoint=target_softpoint,
            action_type="setpoint_hold",
            current_value=current_value,
            suggested_value=value,
            delta=round(value - current_value, 3) if current_value is not None else None,
            unit=unit,
            risk_note=reason or "Manual-mode softpoint command.",
        )
        recommendation = ControlRecommendation(
            id=recommendation_id(line_id, "manual_operator_setpoint", request_id or target_softpoint),
            line_id=line_id,
            level="info",
            diagnosis_code="manual_operator_setpoint",
            title=f"Manual confirmed softpoint command for {target_softpoint}",
            reason=reason or "HMI/operator requested a manual-mode setpoint write through the control orchestrator.",
            evidence=[
                {
                    "signal": "control_state",
                    "value": snapshot.control_state.model_dump(),
                },
                {
                    "signal": "operator_request",
                    "operator_id": operator_id,
                    "request_id": request_id,
                },
                {
                    "tag": feedback_tag,
                    "value": current_value,
                    "signal": "current_feedback",
                },
            ],
            suggested_actions=[action],
            requires_confirmation=True,
            actionable=True,
            blocked_by=[],
            created_at=now,
            expires_at=expires_from(now),
        )
        return apply_recommendation_safety_gates(
            snapshot,
            recommendation,
            allowed_control_modes={"MANUAL"},
            mode_block_code="CONTROL_MODE_NOT_MANUAL",
        )


def _executable_actions(actions: List[SuggestedAction]) -> List[SuggestedAction]:
    return [
        action for action in actions
        if action.is_executable() and action.target_softpoint and action.suggested_value is not None
    ]


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_expired(recommendation: ControlRecommendation) -> bool:
    return recommendation.expires_at <= datetime.now(timezone.utc)


@lru_cache(maxsize=1)
def get_control_orchestrator() -> ControlOrchestrator:
    return ControlOrchestrator()
