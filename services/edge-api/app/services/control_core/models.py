from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


RecommendationLevel = Literal["info", "warning", "critical"]
ActionType = Literal["setpoint_delta", "setpoint_hold", "stop_feed", "inspect"]

SCHEMA_VERSION = "drycontroller.control_recommendation.v1"
RECOMMENDATION_TTL_SECONDS = 300

LEVEL_TAGS = [f"LL{index}" for index in range(1, 7)]
TEMPERATURE_TAGS = ["FTT1", "FTT2", "FTT3", "T_OUT2", "T_OUT4", "TT1", "TT2"]
CURRENT_TAGS = [f"I_M{index}" for index in range(1, 17)]
MOTOR_TAGS = [f"M{index}" for index in range(1, 17)]
FEEDBACK_TAGS = ["VFM1", "VFM2", "VFM3", "VF1", "VF2", "VF3"]
PROCESS_TAGS = LEVEL_TAGS + TEMPERATURE_TAGS + ["MT1"] + CURRENT_TAGS + MOTOR_TAGS + FEEDBACK_TAGS
CONTROL_STATE_TAGS = [
    "CONTROL_MODE",
    "AUTHORITY_OWNER",
    "AUTHORITY_EPOCH",
    "AUTH_TRANSITION_STATE",
    "HMI_DIRECT_ENABLE",
    "MAINTENANCE_LOCK",
    "EMERGENCY_STOP",
]
# DryController only replaces manual writes to the six frequency setpoints.
# Product mode is MANUAL/AUTO/AUTO_PAUSED; PLC authority owner/epoch are
# required before edge-api can ask edge-daq to issue a D5100 write.
SNAPSHOT_TAGS = PROCESS_TAGS + CONTROL_STATE_TAGS
REQUIRED_CONTROL_STATE_TAGS = ["AUTHORITY_OWNER", "AUTHORITY_EPOCH", "AUTH_TRANSITION_STATE", "HMI_DIRECT_ENABLE"]
REQUIRED_SNAPSHOT_TAGS = PROCESS_TAGS + REQUIRED_CONTROL_STATE_TAGS

ALLOWED_CONTROL_SOFTPOINTS = {
    "VFM1_FREQ_SP": (0.0, 50.0, "Hz"),
    "VFM2_FREQ_SP": (0.0, 50.0, "Hz"),
    "VFM3_FREQ_SP": (0.0, 50.0, "Hz"),
    "VF1_FREQ_SP": (0.0, 50.0, "Hz"),
    "VF2_FREQ_SP": (0.0, 50.0, "Hz"),
    "VF3_FREQ_SP": (0.0, 50.0, "Hz"),
}

BOILER_MONITOR_ONLY_TAGS = {"TT1", "TT2", "LL5", "LL6", "M15", "M16"}

MOTOR_FEED_SOFTPOINTS = {
    "M1": "VFM1_FREQ_SP",
    "M2": "VFM1_FREQ_SP",
    "M3": "VFM1_FREQ_SP",
    "M4": "VFM2_FREQ_SP",
    "M5": "VFM2_FREQ_SP",
    "M6": "VFM2_FREQ_SP",
    "M7": "VFM2_FREQ_SP",
    "M8": "VFM2_FREQ_SP",
    "M9": "VFM3_FREQ_SP",
    "M10": "VFM3_FREQ_SP",
    "M11": "VFM3_FREQ_SP",
    "M12": "VFM3_FREQ_SP",
    "M13": "VFM3_FREQ_SP",
    "M14": "VFM3_FREQ_SP",
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
    if not softpoint or not softpoint.endswith("_FREQ_SP"):
        return None
    return softpoint.removesuffix("_FREQ_SP")


def clamp_softpoint_value(softpoint: str, value: float) -> float:
    lo, hi, _unit = ALLOWED_CONTROL_SOFTPOINTS[softpoint]
    return round(min(max(value, lo), hi), 3)


def softpoint_unit(softpoint: str) -> str:
    return ALLOWED_CONTROL_SOFTPOINTS[softpoint][2]
