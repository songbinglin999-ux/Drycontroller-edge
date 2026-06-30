from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


RecommendationLevel = Literal["info", "warning", "critical"]
ActionType = Literal["setpoint_delta", "setpoint_hold", "stop_feed", "inspect"]

SCHEMA_VERSION = "drycontroller.control_recommendation.v1"
RECOMMENDATION_TTL_SECONDS = 300

LEVEL_TAGS = [f"LL{index}" for index in range(1, 7)]
CURRENT_TAGS = [
    "M1_I",
    "M2_I",
    "VFM1_I",
    "M4_I",
    "M3_I",
    "VFM2_I",
    "M6_I",
    "M5_I",
    "M7_I",
    "M8_I",
    "M9_I",
    "VFM3_I",
    "M11_I",
    "M10_I",
    "M12_I",
    "M13_I",
    "M14_I",
    "VF1_I",
    "VF2_I",
    "VF3_I",
    "M15_I",
    "M16_I",
]
TEMPERATURE_TAGS = ["FTT1_T", "FTT2_T", "FTT3_T", "TT1_T", "TT2_T"]
HUMIDITY_TAGS = ["MT1_H"]
ACTIVE_TARGET_TAGS = [
    "VFM1_ACTIVE_FREQ_TARGET",
    "VFM2_ACTIVE_FREQ_TARGET",
    "VFM3_ACTIVE_FREQ_TARGET",
    "VF1_ACTIVE_FREQ_TARGET",
    "VF2_ACTIVE_FREQ_TARGET",
    "VF3_ACTIVE_FREQ_TARGET",
]
PROCESS_TAGS = LEVEL_TAGS + CURRENT_TAGS + TEMPERATURE_TAGS + HUMIDITY_TAGS
CONTROL_STATE_TAGS = [
    "AUTHORITY_OWNER",
    "AUTHORITY_EPOCH",
    "AUTH_TRANSITION_STATE",
    "HMI_DIRECT_ENABLE",
]
# DryController only replaces manual writes to the six frequency setpoints.
# Product mode is MANUAL/AUTO/AUTO_PAUSED; PLC authority owner/epoch are
# required before edge-api can ask edge-daq to issue a D5100 write.
SNAPSHOT_TAGS = PROCESS_TAGS + ACTIVE_TARGET_TAGS + CONTROL_STATE_TAGS
REQUIRED_CONTROL_STATE_TAGS = ["AUTHORITY_OWNER", "AUTHORITY_EPOCH", "AUTH_TRANSITION_STATE", "HMI_DIRECT_ENABLE"]
REQUIRED_SNAPSHOT_TAGS = PROCESS_TAGS + REQUIRED_CONTROL_STATE_TAGS
CONTROL_BASIS_BLOCK = "CONTROL_BASIS_INSUFFICIENT"

ALLOWED_CONTROL_SOFTPOINTS = {
    "VFM1_FREQ_SP": (0.0, 50.0, "Hz"),
    "VFM2_FREQ_SP": (0.0, 50.0, "Hz"),
    "VFM3_FREQ_SP": (0.0, 50.0, "Hz"),
    "VF1_FREQ_SP": (0.0, 50.0, "Hz"),
    "VF2_FREQ_SP": (0.0, 50.0, "Hz"),
    "VF3_FREQ_SP": (0.0, 50.0, "Hz"),
}

BOILER_MONITOR_ONLY_TAGS = {"TT1_T", "TT2_T", "LL5", "LL6", "M15_I", "M16_I"}

CURRENT_FEED_SOFTPOINTS = {
    "M1_I": "VFM1_FREQ_SP",
    "M2_I": "VFM1_FREQ_SP",
    "M3_I": "VFM1_FREQ_SP",
    "VFM1_I": "VFM1_FREQ_SP",
    "M4_I": "VFM2_FREQ_SP",
    "M5_I": "VFM2_FREQ_SP",
    "M6_I": "VFM2_FREQ_SP",
    "M7_I": "VFM2_FREQ_SP",
    "M8_I": "VFM2_FREQ_SP",
    "VFM2_I": "VFM2_FREQ_SP",
    "M9_I": "VFM3_FREQ_SP",
    "M10_I": "VFM3_FREQ_SP",
    "M11_I": "VFM3_FREQ_SP",
    "M12_I": "VFM3_FREQ_SP",
    "M13_I": "VFM3_FREQ_SP",
    "M14_I": "VFM3_FREQ_SP",
    "VFM3_I": "VFM3_FREQ_SP",
}

SOFTPOINT_ACTIVE_TARGET_TAGS = {
    "VFM1_FREQ_SP": "VFM1_ACTIVE_FREQ_TARGET",
    "VFM2_FREQ_SP": "VFM2_ACTIVE_FREQ_TARGET",
    "VFM3_FREQ_SP": "VFM3_ACTIVE_FREQ_TARGET",
    "VF1_FREQ_SP": "VF1_ACTIVE_FREQ_TARGET",
    "VF2_FREQ_SP": "VF2_ACTIVE_FREQ_TARGET",
    "VF3_FREQ_SP": "VF3_ACTIVE_FREQ_TARGET",
}


class SnapshotFreshness(BaseModel):
    status: Literal["fresh", "partial", "missing"] = "fresh"
    missing_tags: List[str] = Field(default_factory=list)
    stale_tags: List[str] = Field(default_factory=list)

    def is_degraded(self) -> bool:
        return self.status != "fresh" or bool(self.missing_tags or self.stale_tags)


class ControlState(BaseModel):
    CONTROL_MODE: Optional[str] = None
    AUTHORITY_OWNER: Optional[str] = None
    AUTHORITY_EPOCH: Optional[int] = None
    AUTH_TRANSITION_STATE: Optional[int] = None
    HMI_DIRECT_ENABLE: Optional[bool] = None
    maintenance_lock: Optional[bool] = None
    emergency_stop: Optional[bool] = None


class TrendPoint(BaseModel):
    time: Optional[str] = None
    value: Optional[float] = None


class ProcessSnapshot(BaseModel):
    line_id: str
    timestamp: datetime
    freshness: SnapshotFreshness
    tags: Dict[str, Any] = Field(default_factory=dict)
    histories: Dict[str, List[TrendPoint]] = Field(default_factory=dict)
    control_state: ControlState


class SuggestedAction(BaseModel):
    target_softpoint: Optional[str] = None
    action_type: ActionType
    current_value: Optional[float] = None
    suggested_value: Optional[float] = None
    delta: Optional[float] = None
    unit: Optional[str] = None
    risk_note: str

    def is_executable(self) -> bool:
        # Executable scope is frequency target setting only. Process actions
        # such as stop_feed remain advisory until their PLC contracts are wired.
        return self.action_type in {"setpoint_delta", "setpoint_hold"}


class ControlRecommendation(BaseModel):
    id: str
    line_id: str
    level: RecommendationLevel
    diagnosis_code: str
    title: str
    reason: str
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    suggested_actions: List[SuggestedAction] = Field(default_factory=list)
    requires_confirmation: bool = False
    actionable: bool = False
    blocked_by: List[str] = Field(default_factory=list)
    created_at: datetime
    expires_at: datetime


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def expires_from(now: datetime) -> datetime:
    return now + timedelta(seconds=RECOMMENDATION_TTL_SECONDS)


def recommendation_id(line_id: str, diagnosis_code: str, suffix: str | None = None) -> str:
    parts = ["rec", line_id.lower(), diagnosis_code.lower()]
    if suffix:
        parts.append(suffix.lower())
    return "_".join(part.replace(".", "_").replace(":", "_") for part in parts)


def softpoint_feedback_tag(softpoint: str | None) -> Optional[str]:
    if not softpoint:
        return None
    return SOFTPOINT_ACTIVE_TARGET_TAGS.get(softpoint)


def clamp_softpoint_value(softpoint: str, value: float) -> float:
    lo, hi, _unit = ALLOWED_CONTROL_SOFTPOINTS[softpoint]
    return round(min(max(value, lo), hi), 3)


def softpoint_unit(softpoint: str) -> str:
    return ALLOWED_CONTROL_SOFTPOINTS[softpoint][2]
