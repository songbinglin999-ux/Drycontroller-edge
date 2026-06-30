from __future__ import annotations

from statistics import mean
from typing import List, Optional

from app.services.control_core.models import (
    CONTROL_BASIS_BLOCK,
    ControlRecommendation,
    SuggestedAction,
    clamp_softpoint_value,
    expires_from,
    recommendation_id,
    softpoint_unit,
    utc_now,
)


def analyze_temperature_strategy(snapshot) -> List[ControlRecommendation]:
    recommendations: List[ControlRecommendation] = []
    ftt_values = [_as_float(snapshot.tags.get(tag)) for tag in ("FTT1_T", "FTT2_T", "FTT3_T")]
    fan_values = [
        _as_float(snapshot.tags.get(tag))
        for tag in ("VF1_ACTIVE_FREQ_TARGET", "VF2_ACTIVE_FREQ_TARGET", "VF3_ACTIVE_FREQ_TARGET")
    ]
    moisture = _as_float(snapshot.tags.get("MT1_H"))
    avg_ftt = mean([value for value in ftt_values if value is not None]) if any(value is not None for value in ftt_values) else None
    avg_fan = mean([value for value in fan_values if value is not None]) if any(value is not None for value in fan_values) else None

    if avg_ftt is not None and avg_ftt <= 58.0:
        recommendations.append(_low_heat_strategy(snapshot, avg_ftt, avg_fan))
    if moisture is not None and moisture >= 16.0:
        recommendations.append(_high_moisture_strategy(snapshot, moisture))
    return recommendations


def _low_heat_strategy(snapshot, avg_ftt: float, avg_fan: Optional[float]) -> ControlRecommendation:
    now = utc_now()
    current_value = _as_float(snapshot.tags.get("VF3_ACTIVE_FREQ_TARGET"))
    suggested_value = (
        clamp_softpoint_value("VF3_FREQ_SP", current_value - 2.0)
        if current_value is not None and avg_fan is not None and avg_fan >= 35.0
        else None
    )
    return ControlRecommendation(
        id=recommendation_id(snapshot.line_id, "temperature_low_heat", "vf3"),
        line_id=snapshot.line_id,
        level="warning",
        diagnosis_code="temperature_low_heat",
        title="Temperature strategy: heat supply is low",
        reason="Average drying temperature is below the advisory threshold; boiler tags are monitor-only.",
        evidence=[
            {"tag": "FTT_AVG", "value": round(avg_ftt, 3), "signal": "low_temperature"},
            {"tag": "TT1_T", "value": snapshot.tags.get("TT1_T"), "signal": "boiler_monitor_only"},
            {"tag": "TT2_T", "value": snapshot.tags.get("TT2_T"), "signal": "boiler_monitor_only"},
        ],
        suggested_actions=[
            SuggestedAction(
                target_softpoint="VF3_FREQ_SP",
                action_type="setpoint_delta",
                current_value=current_value,
                suggested_value=suggested_value,
                delta=-2.0,
                unit=softpoint_unit("VF3_FREQ_SP"),
                risk_note="Advisory only until accepted-target scale and VFD/PV status are signed off.",
            ),
            SuggestedAction(
                action_type="inspect",
                risk_note="Inspect heat supply; do not control TT1_T/TT2_T, LL5/LL6, M15_I, or M16_I from this algorithm.",
            ),
        ],
        actionable=False,
        blocked_by=[CONTROL_BASIS_BLOCK],
        created_at=now,
        expires_at=expires_from(now),
    )


def _high_moisture_strategy(snapshot, moisture: float) -> ControlRecommendation:
    now = utc_now()
    current_fan = _as_float(snapshot.tags.get("VF3_ACTIVE_FREQ_TARGET"))
    suggested_fan = (
        clamp_softpoint_value("VF3_FREQ_SP", current_fan + 2.0)
        if current_fan is not None
        else None
    )
    current_feed = _as_float(snapshot.tags.get("VFM3_ACTIVE_FREQ_TARGET"))
    suggested_feed = (
        clamp_softpoint_value("VFM3_FREQ_SP", current_feed - 1.0)
        if current_feed is not None
        else None
    )
    return ControlRecommendation(
        id=recommendation_id(snapshot.line_id, "temperature_high_moisture", "tail"),
        line_id=snapshot.line_id,
        level="warning",
        diagnosis_code="temperature_high_moisture",
        title="Temperature strategy: moisture is high",
        reason="Tail moisture is high; AUTO may adjust fan/feed softpoints only when gates are clear.",
        evidence=[
            {"tag": "MT1_H", "value": moisture, "signal": "moisture_high"},
        ],
        suggested_actions=[
            SuggestedAction(
                target_softpoint="VF3_FREQ_SP",
                action_type="setpoint_delta",
                current_value=current_fan,
                suggested_value=suggested_fan,
                delta=2.0,
                unit=softpoint_unit("VF3_FREQ_SP"),
                risk_note="Advisory only until airflow/PV status and accepted-target scale are signed off.",
            ),
            SuggestedAction(
                target_softpoint="VFM3_FREQ_SP",
                action_type="setpoint_delta",
                current_value=current_feed,
                suggested_value=suggested_feed,
                delta=-1.0,
                unit=softpoint_unit("VFM3_FREQ_SP"),
                risk_note="Advisory only until feed/PV status and accepted-target scale are signed off.",
            ),
        ],
        actionable=False,
        blocked_by=[CONTROL_BASIS_BLOCK],
        created_at=now,
        expires_at=expires_from(now),
    )


def _as_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
