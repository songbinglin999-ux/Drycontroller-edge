from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from app.services.control_core.models import (
    CURRENT_TAGS,
    PROCESS_TAGS,
    REQUIRED_SNAPSHOT_TAGS,
    SNAPSHOT_TAGS,
    ControlState,
    ProcessSnapshot,
    SnapshotFreshness,
    TrendPoint,
)
from app.services.realtime_data import RealtimeDataService, get_realtime_data_service

HISTORY_WINDOW = "30s"


async def build_process_snapshot(
    *,
    line_id: str,
    lookback: str = "5m",
    realtime_service: Optional[RealtimeDataService] = None,
) -> ProcessSnapshot:
    service = realtime_service or get_realtime_data_service()
    payload = await service.get_latest_payload(
        requested_tags=list(SNAPSHOT_TAGS),
        lookback=lookback,
    )
    records = payload.get("data") or []
    latest = _records_to_map(records)
    missing_tags = _missing_tags(latest.keys(), payload.get("missing") or [])

    histories = await _load_current_histories(service, lookback)
    freshness = SnapshotFreshness(
        status=_freshness_status(records, missing_tags),
        missing_tags=missing_tags,
        stale_tags=[],
    )
    return ProcessSnapshot(
        line_id=line_id,
        timestamp=datetime.now(timezone.utc),
        freshness=freshness,
        tags={tag: latest.get(tag) for tag in PROCESS_TAGS if tag in latest},
        histories=histories,
        control_state=ControlState(
            CONTROL_MODE=_as_str(latest.get("CONTROL_MODE")),
            AUTHORITY_OWNER=_as_authority_owner(latest.get("AUTHORITY_OWNER")),
            AUTHORITY_EPOCH=_as_int(latest.get("AUTHORITY_EPOCH")),
            AUTH_TRANSITION_STATE=_as_int(latest.get("AUTH_TRANSITION_STATE")),
            HMI_DIRECT_ENABLE=_as_bool(latest.get("HMI_DIRECT_ENABLE")),
            maintenance_lock=_as_bool(latest.get("MAINTENANCE_LOCK")),
            emergency_stop=_as_bool(latest.get("EMERGENCY_STOP")),
        ),
    )


def _records_to_map(records: List[dict]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for record in records:
        tag = record.get("tag")
        if tag is None:
            continue
        result[str(tag)] = record.get("value")
    return result


def _missing_tags(present_tags: Iterable[str], payload_missing: List[str]) -> List[str]:
    present = set(present_tags)
    required = set(REQUIRED_SNAPSHOT_TAGS)
    missing = {tag for tag in payload_missing if tag in required}
    missing.update(tag for tag in REQUIRED_SNAPSHOT_TAGS if tag not in present)
    return sorted(missing)


def _freshness_status(records: List[dict], missing_tags: List[str]) -> str:
    if not records:
        return "missing"
    return "partial" if missing_tags else "fresh"


async def _load_current_histories(
    service: RealtimeDataService,
    lookback: str,
) -> Dict[str, List[TrendPoint]]:
    histories: Dict[str, List[TrendPoint]] = {}
    for tag in CURRENT_TAGS:
        payload = await service.get_history_payload(
            tag=tag,
            period=lookback,
            window=HISTORY_WINDOW,
            aggregation="mean",
        )
        histories[tag] = [
            TrendPoint(time=point.get("time"), value=_as_float(point.get("value")))
            for point in payload.get("data", {}).get("points", [])
        ]
    return histories


def _as_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "edge", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return None


def _as_authority_owner(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            return None
        return {
            0: "PLC_LOCAL",
            1: "LOCAL_SCREEN",
            2: "EDGE",
            3: "DRY_HMI_DIRECT",
            4: "MAINTENANCE",
            5: "FAULT",
        }.get(numeric)
    normalized = str(value).strip().upper()
    if normalized in {"2", "EDGE", "EDGE_D5100"}:
        return "EDGE"
    if normalized in {"1", "PLC_SCREEN", "LOCAL", "LOCAL_SCREEN"}:
        return "LOCAL_SCREEN"
    if normalized in {"0", "PLC_LOCAL"}:
        return "PLC_LOCAL"
    if normalized in {"3", "HMI_DIRECT", "DRY_HMI", "DRY_HMI_DIRECT"}:
        return "DRY_HMI_DIRECT"
    if normalized in {"4", "MAINTENANCE"}:
        return "MAINTENANCE"
    if normalized in {"5", "FAULT"}:
        return "FAULT"
    return normalized or None


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value).strip() or None
