from __future__ import annotations

import datetime as dt
from typing import Any

import structlog
from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models import AlarmEvent, CloudSyncOutbox

logger = structlog.get_logger(__name__)


# severity 等级，用于 transition 合并时不让 critical 被 warning 覆盖
_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).isoformat()


def _severity_max(current: str | None, candidate: str | None) -> str:
    a = _SEVERITY_RANK.get((current or "").lower(), -1)
    b = _SEVERITY_RANK.get((candidate or "").lower(), -1)
    return candidate if b >= a and candidate else (current or candidate or "info")


async def write_alarm_event(
    *,
    severity: str,
    source_service: str,
    alarm_key: str,
    tenant_id: str | None = None,
    line_id: str | None = None,
    node_id: str | None = None,
    node_name: str | None = None,
    device_id: str | None = None,
    message: str | None = None,
    state: str | None = None,
    raised_at: dt.datetime | None = None,
    first_seen_at: dt.datetime | None = None,
    last_seen_at: dt.datetime | None = None,
    occurrence_count: int = 1,
    ack_at: dt.datetime | None = None,
    ack_by: str | None = None,
    silenced_until: dt.datetime | None = None,
    resolved_at: dt.datetime | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """
    Append an edge alarm event locally and enqueue the same event for cloud sync.

    Mirrors `write_audit` / `write_control_history`: failures are swallowed after
    logging so business operations are never blocked by alarm persistence issues.
    """
    safe_meta = meta or {}
    now = dt.datetime.now(dt.timezone.utc)
    raised_at = raised_at or now
    first_seen_at = first_seen_at or raised_at
    last_seen_at = last_seen_at or raised_at

    try:
        async with AsyncSessionLocal() as db:
            try:
                row = AlarmEvent(
                    tenant_id=tenant_id,
                    line_id=line_id,
                    node_id=node_id,
                    node_name=node_name,
                    device_id=device_id,
                    severity=severity,
                    source_service=source_service,
                    alarm_key=alarm_key,
                    message=message,
                    state=state,
                    raised_at=raised_at,
                    first_seen_at=first_seen_at,
                    last_seen_at=last_seen_at,
                    occurrence_count=occurrence_count,
                    ack_at=ack_at,
                    ack_by=ack_by,
                    silenced_until=silenced_until,
                    resolved_at=resolved_at,
                    meta=safe_meta,
                )
                db.add(row)
                await db.flush()

                db.add(
                    CloudSyncOutbox(
                        kind="alarm",
                        max_attempts=settings.CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS,
                        payload={
                            "edge_alarm_id": row.id,
                            "tenant_id": tenant_id,
                            "line_id": line_id,
                            "node_id": node_id,
                            "node_name": node_name,
                            "device_id": device_id,
                            "severity": severity,
                            "source_service": source_service,
                            "alarm_key": alarm_key,
                            "message": message,
                            "state": state,
                            "raised_at": _iso(raised_at),
                            "first_seen_at": _iso(first_seen_at),
                            "last_seen_at": _iso(last_seen_at),
                            "occurrence_count": occurrence_count,
                            "ack_at": _iso(ack_at),
                            "ack_by": ack_by,
                            "silenced_until": _iso(silenced_until),
                            "resolved_at": _iso(resolved_at),
                            "meta": safe_meta,
                        },
                    )
                )
                await db.commit()
            except Exception:  # noqa: BLE001 - 故意宽捕：best-effort alarm writer 失败需 rollback 后交给外层吞掉
                await db.rollback()
                raise
    except Exception as exc:  # noqa: BLE001 - 故意宽捕：告警持久化失败不能阻塞业务流程
        logger.error(
            "edge_alarm_event_write_failed",
            alarm_key=alarm_key,
            severity=severity,
            source_service=source_service,
            error=str(exc),
        )


def build_alarm_outbox_payload(row: AlarmEvent) -> dict[str, Any]:
    return {
        "edge_alarm_id": row.id,
        "tenant_id": row.tenant_id,
        "line_id": row.line_id,
        "node_id": row.node_id,
        "node_name": row.node_name,
        "device_id": row.device_id,
        "severity": row.severity,
        "source_service": row.source_service,
        "alarm_key": row.alarm_key,
        "message": row.message,
        "state": row.state,
        "raised_at": _iso(row.raised_at),
        "first_seen_at": _iso(row.first_seen_at),
        "last_seen_at": _iso(row.last_seen_at),
        "occurrence_count": row.occurrence_count,
        "ack_at": _iso(row.ack_at),
        "ack_by": row.ack_by,
        "silenced_until": _iso(row.silenced_until),
        "resolved_at": _iso(row.resolved_at),
        "meta": dict(row.meta or {}),
    }


async def apply_alarm_transition(
    *,
    alarm_key: str,
    source_service: str,
    state: str,
    severity: str | None = None,
    tenant_id: str | None = None,
    line_id: str | None = None,
    node_id: str | None = None,
    node_name: str | None = None,
    device_id: str | None = None,
    message: str | None = None,
    occurred_at: dt.datetime | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    上报一次 alarm 状态转移（firing / resolved），按 alarm_key+tenant+line 做 read-modify-write 去重。

    state == "firing":
      - 查同 (tenant_id, line_id, alarm_key) 中 resolved_at IS NULL 的最新一行（=当前 open firing）。
      - 命中：update last_seen_at、occurrence_count+=1，severity 取更严重值，message/meta 覆盖。
      - 未命中：insert 一行新 firing。
    state == "resolved":
      - 同上查 open firing；命中则 update state="resolved" + resolved_at；未命中则 no-op。

    任一命中/insert 后都写一条 CloudSyncOutbox(kind="alarm") 让 drainer 推到云端。
    no-op 不写 outbox。

    返回 {"action": "inserted"|"updated"|"noop", "alarm_id": int|None}。
    """
    state_lc = (state or "").strip().lower()
    if state_lc not in {"firing", "resolved"}:
        raise ValueError(f"invalid alarm state: {state!r}")

    now = (occurred_at or dt.datetime.now(dt.timezone.utc))
    safe_meta = dict(meta or {})

    try:
        async with AsyncSessionLocal() as db:
            try:
                where_clauses = [
                    AlarmEvent.alarm_key == alarm_key,
                    AlarmEvent.resolved_at.is_(None),
                ]
                if tenant_id is None:
                    where_clauses.append(AlarmEvent.tenant_id.is_(None))
                else:
                    where_clauses.append(AlarmEvent.tenant_id == tenant_id)
                if line_id is None:
                    where_clauses.append(AlarmEvent.line_id.is_(None))
                else:
                    where_clauses.append(AlarmEvent.line_id == line_id)
                stmt = (
                    select(AlarmEvent)
                    .where(*where_clauses)
                    .order_by(AlarmEvent.raised_at.desc(), AlarmEvent.id.desc())
                    .limit(1)
                )
                result = await db.execute(stmt)
                existing = result.scalars().all()
                existing_row = existing[0] if existing else None

                if state_lc == "firing":
                    if existing_row is None:
                        row = AlarmEvent(
                            tenant_id=tenant_id,
                            line_id=line_id,
                            node_id=node_id,
                            node_name=node_name,
                            device_id=device_id,
                            severity=(severity or "info"),
                            source_service=source_service,
                            alarm_key=alarm_key,
                            message=message,
                            state="firing",
                            raised_at=now,
                            first_seen_at=now,
                            last_seen_at=now,
                            occurrence_count=1,
                            meta=safe_meta,
                        )
                        db.add(row)
                        await db.flush()
                        outbox = CloudSyncOutbox(
                            kind="alarm",
                            max_attempts=settings.CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS,
                            payload=build_alarm_outbox_payload(row),
                        )
                        db.add(outbox)
                        await db.commit()
                        return {"action": "inserted", "alarm_id": row.id}

                    existing_row.last_seen_at = now
                    existing_row.occurrence_count = int(existing_row.occurrence_count or 0) + 1
                    existing_row.severity = _severity_max(existing_row.severity, severity)
                    if message is not None:
                        existing_row.message = message
                    if safe_meta:
                        existing_row.meta = safe_meta
                    if node_id is not None:
                        existing_row.node_id = node_id
                    if node_name is not None:
                        existing_row.node_name = node_name
                    if device_id is not None:
                        existing_row.device_id = device_id
                    existing_row.state = "firing"
                    db.add(
                        CloudSyncOutbox(
                            kind="alarm",
                            max_attempts=settings.CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS,
                            payload=build_alarm_outbox_payload(existing_row),
                        )
                    )
                    await db.commit()
                    return {"action": "updated", "alarm_id": existing_row.id}

                # state_lc == "resolved"
                if existing_row is None:
                    await db.rollback()
                    return {"action": "noop", "alarm_id": None}
                existing_row.state = "resolved"
                existing_row.resolved_at = now
                existing_row.last_seen_at = now
                db.add(
                    CloudSyncOutbox(
                        kind="alarm",
                        max_attempts=settings.CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS,
                        payload=build_alarm_outbox_payload(existing_row),
                    )
                )
                await db.commit()
                return {"action": "updated", "alarm_id": existing_row.id}
            except Exception:  # noqa: BLE001 - 故意宽捕：best-effort alarm transition 失败需 rollback 后交给外层吞掉
                await db.rollback()
                raise
    except Exception as exc:  # noqa: BLE001 - 故意宽捕：告警状态同步失败不能阻塞业务流程
        logger.error(
            "edge_alarm_transition_failed",
            alarm_key=alarm_key,
            severity=severity,
            state=state_lc,
            source_service=source_service,
            error=str(exc),
        )
        return {"action": "error", "alarm_id": None, "error": str(exc)}


async def ack_alarm_event(
    *,
    alarm_id: int,
    operator: str,
    note: str | None,
) -> dict[str, Any]:
    """
    标记 alarm_id 为 acknowledged，写 outbox 让云端镜像。

    Idempotent：重复 ACK 仍 update（覆盖 ack_at / ack_by / meta.note）+ 写 outbox。
    alarm_id 不存在时返回 {"action": "not_found"}。
    """
    now = dt.datetime.now(dt.timezone.utc)
    safe_operator = (operator or "unknown").strip()[:128] or "unknown"
    safe_note = note.strip()[:256] if note and note.strip() else None

    try:
        async with AsyncSessionLocal() as db:
            try:
                stmt = select(AlarmEvent).where(AlarmEvent.id == alarm_id).limit(1)
                rs = await db.execute(stmt)
                rows = rs.scalars().all()
                if not rows:
                    await db.rollback()
                    return {"action": "not_found", "alarm_id": alarm_id}
                row = rows[0]
                row.ack_at = now
                row.ack_by = safe_operator
                if safe_note is not None:
                    meta = dict(row.meta or {})
                    meta["note"] = safe_note
                    row.meta = meta
                db.add(
                    CloudSyncOutbox(
                        kind="alarm",
                        max_attempts=settings.CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS,
                        payload=build_alarm_outbox_payload(row),
                    )
                )
                await db.commit()
                return {"action": "acknowledged", "alarm_id": row.id}
            except Exception:  # noqa: BLE001 - 故意宽捕：best-effort alarm ack 失败需 rollback 后交给外层吞掉
                await db.rollback()
                raise
    except Exception as exc:  # noqa: BLE001 - 故意宽捕：告警确认持久化失败不能阻塞业务流程
        logger.error(
            "edge_alarm_ack_failed",
            alarm_id=alarm_id,
            error=str(exc),
        )
        return {"action": "error", "alarm_id": alarm_id, "error": str(exc)}
