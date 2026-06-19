from __future__ import annotations

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


def analyze_graded_control(snapshot) -> List[ControlRecommendation]:
    high_currents = [
        tag for tag in (f"I_M{index}" for index in range(1, 15))
        if _as_float(snapshot.tags.get(tag), 0.0) >= 45.0
    ]
    moisture = _as_float(snapshot.tags.get("MT1"))
    low_temperatures = [
        tag for tag in ("FTT1", "FTT2", "FTT3")
        if _as_float(snapshot.tags.get(tag), 999.0) <= 55.0
    ]

    if len(high_currents) >= 3:
        return [_l3_stop_feed(snapshot, high_currents)]
    if high_currents or (moisture is not None and moisture >= 16.0 and low_temperatures):
        return [_l2_adjust_feed(snapshot, high_currents, moisture, low_temperatures)]
    if moisture is not None and moisture >= 14.5:
        return [_l1_monitor(snapshot, moisture)]
    return [_l0_normal(snapshot)]


def _l3_stop_feed(snapshot, high_currents: List[str]) -> ControlRecommendation:
    now = utc_now()
    current_value = _as_float(snapshot.tags.get("VFM3"))
    suggested_value = 0.0 if current_value is not None else None
    return ControlRecommendation(
        id=recommendation_id(snapshot.line_id, "graded_l3_stop_feed", "vfm3"),
        line_id=snapshot.line_id,
        level="critical",
        diagnosis_code="graded_l3_stop_feed",
        title="L3 load condition: stop feed recommendation",
        reason="Multiple current channels are high; V1 AUTO should suspend before any high-risk feed stop action.",
        evidence=[{"tag": tag, "value": snapshot.tags.get(tag), "signal": "high_current"} for tag in high_currents],
        suggested_actions=[
            SuggestedAction(
                target_softpoint="VFM3_FREQ_SP",
                action_type="stop_feed",
                current_value=current_value,
                suggested_value=suggested_value,
                delta=(-current_value if current_value is not None else None),
                unit=softpoint_unit("VFM3_FREQ_SP"),
                risk_note="Do not auto-stop feed in V1; suspend AUTO and require downstream inspection.",
            )
        ],
        actionable=True,
        created_at=now,
        expires_at=expires_from(now),
    )


def _l2_adjust_feed(
    snapshot,
    high_currents: List[str],
    moisture: Optional[float],
    low_temperatures: List[str],
) -> ControlRecommendation:
    now = utc_now()
    current_value = _as_float(snapshot.tags.get("VFM3"))
    suggested_value = (
        clamp_softpoint_value("VFM3_FREQ_SP", current_value - 1.0)
        if current_value is not None
        else None
    )
    evidence = [{"tag": tag, "value": snapshot.tags.get(tag), "signal": "high_current"} for tag in high_currents]
    if moisture is not None:
        evidence.append({"tag": "MT1", "value": moisture, "signal": "moisture"})
    evidence.extend({"tag": tag, "value": snapshot.tags.get(tag), "signal": "low_temperature"} for tag in low_temperatures)
    return ControlRecommendation(
        id=recommendation_id(snapshot.line_id, "graded_l2_adjust_feed", "vfm3"),
        line_id=snapshot.line_id,
        level="warning",
        diagnosis_code="graded_l2_adjust_feed",
        title="L2 condition: reduce feed slightly",
        reason="Load, temperature, or moisture deviation is above the advisory threshold.",
        evidence=evidence,
        suggested_actions=[
            SuggestedAction(
                target_softpoint="VFM3_FREQ_SP",
                action_type="setpoint_delta",
                current_value=current_value,
                suggested_value=suggested_value,
                delta=-1.0,
                unit=softpoint_unit("VFM3_FREQ_SP"),
                risk_note="Small feed reduction candidate for AUTO only while all gates are clear.",
            )
        ],
        actionable=True,
        created_at=now,
        expires_at=expires_from(now),
    )


def _l1_monitor(snapshot, moisture: float) -> ControlRecommendation:
    now = utc_now()
    return ControlRecommendation(
        id=recommendation_id(snapshot.line_id, "graded_l1_monitor", "moisture"),
        line_id=snapshot.line_id,
        level="info",
        diagnosis_code="graded_l1_monitor",
        title="L1 condition: monitor moisture",
        reason="Moisture is mildly above target; no setpoint change is recommended yet.",
        evidence=[{"tag": "MT1", "value": moisture, "signal": "moisture"}],
        suggested_actions=[
            SuggestedAction(action_type="inspect", risk_note="Keep monitoring moisture and downstream load.")
        ],
        actionable=False,
        created_at=now,
        expires_at=expires_from(now),
    )


def _l0_normal(snapshot) -> ControlRecommendation:
    now = utc_now()
    return ControlRecommendation(
        id=recommendation_id(snapshot.line_id, "graded_l0_normal"),
        line_id=snapshot.line_id,
        level="info",
        diagnosis_code="graded_l0_normal",
        title="L0 condition: monitor",
        reason="No graded control threshold is crossed.",
        evidence=[],
        suggested_actions=[
            SuggestedAction(action_type="inspect", risk_note="No control action is suggested.")
        ],
        actionable=False,
        created_at=now,
        expires_at=expires_from(now),
    )


def _as_float(value, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
