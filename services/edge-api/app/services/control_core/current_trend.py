from __future__ import annotations

from statistics import mean
from typing import Any, Dict, List, Optional

from app.services.control_core.models import (
    CURRENT_TAGS,
    MOTOR_FEED_SOFTPOINTS,
    MOTOR_TAGS,
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
    for current_tag, motor_tag in zip(CURRENT_TAGS, MOTOR_TAGS):
        if motor_tag in {"M15", "M16"}:
            continue
        current = _as_float(snapshot.tags.get(current_tag))
        running = _as_bool(snapshot.tags.get(motor_tag))
        values = [
            point.value
            for point in snapshot.histories.get(current_tag, [])
            if point.value is not None
        ]
        baseline = mean(values[:-1] or values) if values else None
        first_value = values[0] if values else None
        delta = current - first_value if current is not None and first_value is not None else None

        if running is True and current is not None and _is_high_load(current, baseline, delta):
            recommendations.append(_high_load_recommendation(snapshot, motor_tag, current_tag, current, baseline, delta))
            continue

        if running is True and current is not None and _is_low_load(current, baseline):
            recommendations.append(_low_load_recommendation(snapshot, motor_tag, current_tag, current, baseline))
            continue

        if running is True and current is not None and current <= 0.3:
            recommendations.append(_no_current_after_start_recommendation(snapshot, motor_tag, current_tag, current))

    return recommendations


def _high_load_recommendation(
    snapshot,
    motor_tag: str,
    current_tag: str,
    current: float,
    baseline: Optional[float],
    delta: Optional[float],
) -> ControlRecommendation:
    now = utc_now()
    softpoint = MOTOR_FEED_SOFTPOINTS.get(motor_tag)
    action = _delta_action(
        snapshot=snapshot,
        softpoint=softpoint,
        delta=-2.0,
        risk_note="Reduce upstream feed only after operator confirms blockage risk.",
    )
    return ControlRecommendation(
        id=recommendation_id(snapshot.line_id, "current_trend_high_load", motor_tag),
        line_id=snapshot.line_id,
        level="critical" if current >= 55 else "warning",
        diagnosis_code="current_trend_high_load",
        title=f"{motor_tag} load is rising",
        reason="Current trend indicates overload or downstream blockage risk.",
        evidence=[
            _evidence(current_tag, current, "current", baseline=baseline, delta=delta),
            _evidence(motor_tag, snapshot.tags.get(motor_tag), "motor_feedback"),
        ],
        suggested_actions=[
            action,
            SuggestedAction(
                action_type="inspect",
                risk_note=f"Inspect downstream material path near {motor_tag} before changing feed.",
            ),
        ],
        actionable=True,
        created_at=now,
        expires_at=expires_from(now),
    )


def _low_load_recommendation(
    snapshot,
    motor_tag: str,
    current_tag: str,
    current: float,
    baseline: Optional[float],
) -> ControlRecommendation:
    now = utc_now()
    return ControlRecommendation(
        id=recommendation_id(snapshot.line_id, "current_trend_empty_run", motor_tag),
        line_id=snapshot.line_id,
        level="warning",
        diagnosis_code="current_trend_empty_run",
        title=f"{motor_tag} may be running empty",
        reason="Motor feedback is running while current is far below its recent baseline.",
        evidence=[
            _evidence(current_tag, current, "current", baseline=baseline),
            _evidence(motor_tag, snapshot.tags.get(motor_tag), "motor_feedback"),
        ],
        suggested_actions=[
            SuggestedAction(
                action_type="inspect",
                risk_note=f"Check upstream supply before stopping or restarting {motor_tag}.",
            )
        ],
        actionable=False,
        created_at=now,
        expires_at=expires_from(now),
    )


def _no_current_after_start_recommendation(
    snapshot,
    motor_tag: str,
    current_tag: str,
    current: float,
) -> ControlRecommendation:
    now = utc_now()
    return ControlRecommendation(
        id=recommendation_id(snapshot.line_id, "current_trend_no_current_after_start", motor_tag),
        line_id=snapshot.line_id,
        level="critical",
        diagnosis_code="current_trend_no_current_after_start",
        title=f"{motor_tag} has no current after start",
        reason="Motor run feedback is active but measured current is near zero.",
        evidence=[
            _evidence(current_tag, current, "current"),
            _evidence(motor_tag, snapshot.tags.get(motor_tag), "motor_feedback"),
        ],
        suggested_actions=[
            SuggestedAction(
                action_type="inspect",
                risk_note="Check contactor, VFD, wiring, and sensor path; do not issue automatic restart.",
            )
        ],
        actionable=False,
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


def _is_low_load(current: float, baseline: Optional[float]) -> bool:
    if baseline is None or baseline <= 1.0:
        return False
    return current <= baseline * 0.45


def _evidence(tag: str, value: Any, signal: str, **extra: Any) -> Dict[str, Any]:
    result = {"tag": tag, "value": value, "signal": signal}
    result.update({key: value for key, value in extra.items() if value is not None})
    return result


def _as_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
