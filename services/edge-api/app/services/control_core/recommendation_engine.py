from __future__ import annotations

from typing import List

from app.services.control_core.alarm_diagnosis import analyze_alarm_diagnosis
from app.services.control_core.current_trend import analyze_current_trend
from app.services.control_core.graded_control import analyze_graded_control
from app.services.control_core.models import (
    ControlRecommendation,
    ProcessSnapshot,
    SuggestedAction,
    expires_from,
    recommendation_id,
    utc_now,
)
from app.services.control_core.safety import apply_recommendation_safety_gates
from app.services.control_core.temperature_strategy import analyze_temperature_strategy


def build_recommendations(snapshot: ProcessSnapshot) -> List[ControlRecommendation]:
    recommendations: List[ControlRecommendation] = []
    if snapshot.freshness.is_degraded():
        recommendations.append(_data_degraded_recommendation(snapshot))

    recommendations.extend(analyze_current_trend(snapshot))
    recommendations.extend(analyze_graded_control(snapshot))
    recommendations.extend(analyze_temperature_strategy(snapshot))
    recommendations.extend(analyze_alarm_diagnosis(snapshot))

    return [
        apply_recommendation_safety_gates(
            snapshot,
            item,
            allowed_control_modes={"SEMI_AUTO"},
            mode_block_code="CONTROL_MODE_NOT_SEMI_AUTO",
        )
        for item in _dedupe(recommendations)
    ]


def _data_degraded_recommendation(snapshot: ProcessSnapshot) -> ControlRecommendation:
    now = utc_now()
    evidence = [
        {"tag": tag, "signal": "missing_or_stale"}
        for tag in (snapshot.freshness.missing_tags + snapshot.freshness.stale_tags)[:20]
    ]
    return ControlRecommendation(
        id=recommendation_id(snapshot.line_id, "data_degraded"),
        line_id=snapshot.line_id,
        level="warning",
        diagnosis_code="data_degraded",
        title="Data is incomplete; executable advice is disabled",
        reason="At least one required snapshot tag is missing or stale, so algorithms only emit diagnostics.",
        evidence=evidence,
        suggested_actions=[
            SuggestedAction(
                action_type="inspect",
                risk_note="Restore realtime tags/control-state signals before confirming any setpoint action.",
            )
        ],
        actionable=False,
        blocked_by=["DATA_MISSING_OR_STALE"],
        created_at=now,
        expires_at=expires_from(now),
    )


def _dedupe(items: List[ControlRecommendation]) -> List[ControlRecommendation]:
    result = {}
    for item in items:
        result[item.id] = item
    return list(result.values())
