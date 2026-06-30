from __future__ import annotations

from statistics import mean
from typing import Any, Dict, List, Optional

from app.services.control_core.models import (
    CONTROL_BASIS_BLOCK,
    CURRENT_FEED_SOFTPOINTS,
    CURRENT_TAGS,
    ControlRecommendation,
    SuggestedAction,
    clamp_softpoint_value,
    expires_from,
    recommendation_id,
    softpoint_feedback_tag,
    softpoint_unit,
    utc_now,
)


def analyze_current_trend(snapshot) -> List[ControlRecommendation]:
    recommendations: List[ControlRecommendation] = []
    for current_tag in CURRENT_TAGS:
        if current_tag in {"M15_I", "M16_I"}:
            continue
        current = _as_float(snapshot.tags.get(current_tag))
        values = [
            point.value
            for point in snapshot.histories.get(current_tag, [])
            if point.value is not None
        ]
        baseline = mean(values[:-1] or values) if values else None
        first_value = values[0] if values else None
        delta = current - first_value if current is not None and first_value is not None else None

        if current is not None and _is_high_load(current, baseline, delta):
            recommendations.append(_high_load_recommendation(snapshot, current_tag, current, baseline, delta))

    return recommendations


def _high_load_recommendation(
    snapshot,
    current_tag: str,
    current: float,
    baseline: Optional[float],
    delta: Optional[float],
) -> ControlRecommendation:
    now = utc_now()
    softpoint = CURRENT_FEED_SOFTPOINTS.get(current_tag)
    action = _delta_action(
        snapshot=snapshot,
        softpoint=softpoint,
        delta=-2.0,
        risk_note="Advisory only until run-state, accepted-target scale, and VFD/PV status are signed off.",
    )
    return ControlRecommendation(
        id=recommendation_id(snapshot.line_id, "current_trend_high_load", current_tag),
        line_id=snapshot.line_id,
        level="critical" if current >= 55 else "warning",
        diagnosis_code="current_trend_high_load",
        title=f"{current_tag} load is rising",
        reason="Current trend indicates possible overload or downstream blockage risk; run feedback is not available in the real 28D+6 table.",
        evidence=[
            _evidence(current_tag, current, "current", baseline=baseline, delta=delta),
        ],
        suggested_actions=[
            action,
            SuggestedAction(
                action_type="inspect",
                risk_note=f"Inspect downstream material path near {current_tag} if the trend persists.",
            ),
        ],
        actionable=False,
        blocked_by=[CONTROL_BASIS_BLOCK],
        created_at=now,
        expires_at=expires_from(now),
    )


def _delta_action(snapshot, softpoint: Optional[str], delta: float, risk_note: str) -> SuggestedAction:
    if not softpoint:
        return SuggestedAction(action_type="inspect", risk_note=risk_note)
    feedback_tag = softpoint_feedback_tag(softpoint)
    current_value = _as_float(snapshot.tags.get(feedback_tag)) if feedback_tag else None
    suggested_value = (
        clamp_softpoint_value(softpoint, current_value + delta)
        if current_value is not None
        else None
    )
    return SuggestedAction(
        target_softpoint=softpoint,
        action_type="setpoint_delta",
        current_value=current_value,
        suggested_value=suggested_value,
        delta=delta,
        unit=softpoint_unit(softpoint),
        risk_note=risk_note,
    )


def _is_high_load(current: float, baseline: Optional[float], delta: Optional[float]) -> bool:
    if current >= 45:
        return True
    if baseline is None or baseline <= 0:
        return False
    return current >= baseline * 1.35 or (delta is not None and delta >= max(2.0, baseline * 0.2))


def _evidence(tag: str, value: Any, signal: str, **extra: Any) -> Dict[str, Any]:
    result = {"tag": tag, "value": value, "signal": signal}
    result.update({key: value for key, value in extra.items() if value is not None})
    return result
def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
