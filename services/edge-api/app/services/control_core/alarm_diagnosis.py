from __future__ import annotations

from typing import List

from app.services.control_core.models import (
    ControlRecommendation,
    SuggestedAction,
    expires_from,
    recommendation_id,
    utc_now,
)


def analyze_alarm_diagnosis(snapshot) -> List[ControlRecommendation]:
    recommendations: List[ControlRecommendation] = []
    for tag in ("LL5", "LL6"):
        if _is_active(snapshot.tags.get(tag)):
            recommendations.append(_boiler_level_monitor(snapshot, tag))
    return recommendations


def _boiler_level_monitor(snapshot, tag: str) -> ControlRecommendation:
    now = utc_now()
    return ControlRecommendation(
        id=recommendation_id(snapshot.line_id, "alarm_boiler_level_monitor", tag),
        line_id=snapshot.line_id,
        level="info",
        diagnosis_code="alarm_boiler_level_monitor",
        title=f"{tag} boiler level signal is active",
        reason="Boiler level is monitor-only in the first advisory control phase.",
        evidence=[
            {"tag": tag, "value": snapshot.tags.get(tag), "signal": "boiler_monitor_only"},
            {"tag": "M15_I", "value": snapshot.tags.get("M15_I"), "signal": "boiler_feed_current_only"},
            {"tag": "M16_I", "value": snapshot.tags.get("M16_I"), "signal": "boiler_feed_current_only"},
        ],
        suggested_actions=[
            SuggestedAction(
                action_type="inspect",
                risk_note="Notify operator; do not write boiler softpoints or M15_I/M16_I from algorithm control.",
            )
        ],
        actionable=False,
        created_at=now,
        expires_at=expires_from(now),
    )


def _is_active(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "active", "on", "alarm"}
