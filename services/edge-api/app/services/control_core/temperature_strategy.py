from __future__ import annotations

from statistics import mean
from typing import List, Optional

from app.services.control_core.models import (
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
    ftt_values = [_as_float(snapshot.tags.get(tag)) for tag in ("FTT1", "FTT2", "FTT3")]
    fan_values = [_as_float(snapshot.tags.get(tag)) for tag in ("VF1", "VF2", "VF3")]
    moisture = _as_float(snapshot.tags.get("MT1"))
    avg_ftt = mean([value for value in ftt_values if value is not None]) if any(value is not None for value in ftt_values) else None
    avg_fan = mean([value for value in fan_values if value is not None]) if any(value is not None for value in fan_values) else None

    if avg_ftt is not None and avg_ftt <= 58.0:
        recommendations.append(_low_heat_strategy(snapshot, avg_ftt, avg_fan))
    if moisture is not None and moisture >= 16.0:
        recommendations.append(_high_moisture_strategy(snapshot, moisture))
    if _as_float(snapshot.tags.get("T_OUT4")) is not None and moisture is not None and moisture >= 15.0:
        recommendations.append(_tail_heavy_strategy(snapshot, moisture))
    return recommendations


def _low_heat_strategy(snapshot, avg_ftt: float, avg_fan: Optional[float]) -> ControlRecommendation:
    now = utc_now()
    current_value = _as_float(snapshot.tags.get("VF3"))
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
            {"tag": "TT1", "value": snapshot.tags.get("TT1"), "signal": "boiler_monitor_only"},
            {"tag": "TT2", "value": snapshot.tags.get("TT2"), "signal": "boiler_monitor_only"},
        ],
        suggested_actions=[
            SuggestedAction(
                target_softpoint="VF3_FREQ_SP",
                action_type="setpoint_delta",
                current_value=current_value,
                suggested_value=suggested_value,
                delta=-2.0,
                unit=softpoint_unit("VF3_FREQ_SP"),
                risk_note="Reduce final-stage airflow only if operator confirms temperature is being pulled down by airflow.",
            ),
            SuggestedAction(
                action_type="inspect",
                risk_note="Inspect heat supply; do not control TT1/TT2, LL5/LL6, M15, or M16 from this algorithm.",
            ),
        ],
        actionable=True,
        created_at=now,
        expires_at=expires_from(now),
    )


def _high_moisture_strategy(snapshot, moisture: float) -> ControlRecommendation:
    now = utc_now()
    current_fan = _as_float(snapshot.tags.get("VF3"))
    suggested_fan = (
        clamp_softpoint_value("VF3_FREQ_SP", current_fan + 2.0)
        if current_fan is not None
        else None
    )
    current_feed = _as_float(snapshot.tags.get("VFM3"))
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
        reason="Tail moisture is high; adjust fan/feed softpoints only after confirmation.",
        evidence=[
            {"tag": "MT1", "value": moisture, "signal": "moisture_high"},
            {"tag": "T_OUT4", "value": snapshot.tags.get("T_OUT4"), "signal": "tail_temperature"},
        ],
        suggested_actions=[
            SuggestedAction(
                target_softpoint="VF3_FREQ_SP",
                action_type="setpoint_delta",
                current_value=current_fan,
                suggested_value=suggested_fan,
                delta=2.0,
                unit=softpoint_unit("VF3_FREQ_SP"),
                risk_note="Increase exhaust airflow only after checking temperature margin.",
            ),
            SuggestedAction(
                target_softpoint="VFM3_FREQ_SP",
                action_type="setpoint_delta",
                current_value=current_feed,
                suggested_value=suggested_feed,
                delta=-1.0,
                unit=softpoint_unit("VFM3_FREQ_SP"),
                risk_note="Limit final feed if high moisture persists.",
            ),
        ],
        actionable=True,
        created_at=now,
        expires_at=expires_from(now),
    )


def _tail_heavy_strategy(snapshot, moisture: float) -> ControlRecommendation:
    now = utc_now()
    return ControlRecommendation(
        id=recommendation_id(snapshot.line_id, "temperature_tail_heavy", "inspect"),
        line_id=snapshot.line_id,
        level="info",
        diagnosis_code="temperature_tail_heavy",
        title="Temperature strategy: tail section needs inspection",
        reason="Tail moisture and outlet temperature suggest possible wet material accumulation.",
        evidence=[
            {"tag": "MT1", "value": moisture, "signal": "moisture_high"},
            {"tag": "T_OUT4", "value": snapshot.tags.get("T_OUT4"), "signal": "tail_temperature"},
        ],
        suggested_actions=[
            SuggestedAction(action_type="inspect", risk_note="Inspect final stage material flow and exhaust path.")
        ],
        actionable=False,
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
