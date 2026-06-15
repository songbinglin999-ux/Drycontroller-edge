from __future__ import annotations

from collections.abc import Iterable
from typing import List, Optional

from app.services.control_core.models import (
    ALLOWED_CONTROL_SOFTPOINTS,
    BOILER_MONITOR_ONLY_TAGS,
    ControlRecommendation,
    ProcessSnapshot,
    SuggestedAction,
)

_ACTION_ERROR_BY_BLOCK = {
    "EXECUTABLE_ACTION_MISSING_TARGET": "missing_target_softpoint",
    "BOILER_CONTROL_FORBIDDEN": "boiler_control_forbidden",
    "MOTOR_FEEDBACK_WRITE_FORBIDDEN": "motor_feedback_write_forbidden",
    "SOFTPOINT_NOT_ALLOWED": "softpoint_not_allowed",
    "SUGGESTED_VALUE_MISSING": "suggested_value_missing",
    "SETPOINT_OUT_OF_RANGE": "setpoint_out_of_range",
}


def apply_recommendation_safety_gates(
    snapshot: ProcessSnapshot,
    recommendation: ControlRecommendation,
    *,
    allowed_control_modes: Iterable[str],
    mode_block_code: str,
) -> ControlRecommendation:
    blocked_by = list(
        dict.fromkeys(
            recommendation.blocked_by
            + snapshot_blocks(
                snapshot,
                allowed_control_modes=allowed_control_modes,
                mode_block_code=mode_block_code,
            )
        )
    )
    actionable = recommendation.actionable
    executable_actions = [action for action in recommendation.suggested_actions if action.is_executable()]

    if not executable_actions:
        actionable = False
    for action in executable_actions:
        blocked_by.extend(block for block in action_blocks(action) if block not in blocked_by)
    if blocked_by:
        actionable = False

    return recommendation.model_copy(
        update={
            "actionable": actionable,
            "blocked_by": blocked_by,
            "requires_confirmation": True,
        }
    )


def snapshot_blocks(
    snapshot: ProcessSnapshot,
    *,
    allowed_control_modes: Iterable[str],
    mode_block_code: str,
) -> List[str]:
    blocks: List[str] = []
    if snapshot.freshness.is_degraded():
        blocks.append("DATA_MISSING_OR_STALE")
    state = snapshot.control_state
    if state.CONTROL_MODE not in set(allowed_control_modes):
        blocks.append(mode_block_code)
    if state.AUTHORITY_OWNER != "EDGE":
        blocks.append("AUTHORITY_OWNER_NOT_EDGE")
    if state.HMI_DIRECT_ENABLE is not False:
        blocks.append("HMI_DIRECT_ENABLE_NOT_FALSE")
    if state.maintenance_lock is not False:
        blocks.append("MAINTENANCE_LOCK_ACTIVE_OR_UNKNOWN")
    if state.emergency_stop is not False:
        blocks.append("EMERGENCY_STOP_ACTIVE_OR_UNKNOWN")
    return blocks


def action_blocks(action: SuggestedAction) -> List[str]:
    if not action.is_executable():
        return []
    if not action.target_softpoint:
        return ["EXECUTABLE_ACTION_MISSING_TARGET"]
    if action.target_softpoint in BOILER_MONITOR_ONLY_TAGS:
        return ["BOILER_CONTROL_FORBIDDEN"]
    if action.target_softpoint.startswith("M"):
        return ["MOTOR_FEEDBACK_WRITE_FORBIDDEN"]
    if action.target_softpoint not in ALLOWED_CONTROL_SOFTPOINTS:
        return ["SOFTPOINT_NOT_ALLOWED"]
    if action.suggested_value is None:
        return ["SUGGESTED_VALUE_MISSING"]
    lo, hi, _unit = ALLOWED_CONTROL_SOFTPOINTS[action.target_softpoint]
    if action.suggested_value < lo or action.suggested_value > hi:
        return ["SETPOINT_OUT_OF_RANGE"]
    return []


def action_validation_error(action: SuggestedAction) -> Optional[str]:
    blocks = action_blocks(action)
    if not blocks:
        return None
    return _ACTION_ERROR_BY_BLOCK.get(blocks[0], blocks[0].lower())
