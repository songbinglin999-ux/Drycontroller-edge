"""
报警管理 API（读 + 内部 ingest + ACK）

读路径 (/active / /history) 现从 alarm_events 表查询，不再走 Influx system_health。
"""

import datetime as dt
import re
import time
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin_api_key
from app.models import AlarmEvent
from app.services.alarm_event import ack_alarm_event, apply_alarm_transition

router = APIRouter()
logger = structlog.get_logger(__name__)


_PERIOD_RE = re.compile(r"^(\d+)([smhd])$")
_PERIOD_UNIT_SEC = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_period(period: str) -> dt.datetime:
    """把 "10m" / "24h" / "30s" / "7d" 翻译成 since (utcnow - period)。"""
    m = _PERIOD_RE.match((period or "").strip())
    if not m:
        raise ValueError(f"invalid period: {period!r}")
    value = int(m.group(1))
    unit = m.group(2)
    if value <= 0:
        raise ValueError(f"invalid period: {period!r}")
    seconds = value * _PERIOD_UNIT_SEC[unit]
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)


def _serialize_alarm(row: AlarmEvent) -> dict[str, Any]:
    def _iso(value):
        return value.astimezone(dt.timezone.utc).isoformat() if value else None
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "line_id": row.line_id,
        "node_id": row.node_id,
        "node_name": row.node_name,
        "device_id": row.device_id,
        "severity": row.severity,
        "source_service": row.source_service,
        "alarm_key": row.alarm_key,
        "state": row.state,
        "message": row.message,
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


@router.get("/active")
async def get_active_alarms(
    period: str = Query("10m", description="时间范围，例如 10m / 24h / 7d"),
    severity: Optional[str] = Query(None, description="筛选 severity：warning/critical"),
    db: AsyncSession = Depends(get_db),
):
    """
    获取当前活动报警（resolved_at IS NULL 且 raised_at 在 period 内）。
    """
    try:
        since = _parse_period(period)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    where_clauses = [
        AlarmEvent.resolved_at.is_(None),
        AlarmEvent.raised_at >= since,
    ]
    if severity:
        where_clauses.append(AlarmEvent.severity == severity)
    stmt = (
        select(AlarmEvent)
        .where(*where_clauses)
        .order_by(AlarmEvent.raised_at.desc(), AlarmEvent.id.desc())
    )
    rs = await db.execute(stmt)
    rows = list(rs.scalars().all())
    return {
        "success": True,
        "count": len(rows),
        "data": [_serialize_alarm(r) for r in rows],
        "timestamp": int(time.time()),
    }


class AlarmAckRequest(BaseModel):
    operator: str = Field("unknown", min_length=1, max_length=128)
    note: Optional[str] = Field(None, max_length=512)


@router.post("/{alarm_id}/acknowledge")
async def acknowledge_alarm(
    alarm_id: int,
    payload: Optional[AlarmAckRequest] = None,
    _: str = Depends(require_admin_api_key),
):
    """
    确认报警：标记 alarm_id 为 acknowledged，并通过 outbox 同步到云端。

    operator 是调用方声明的显示名（不是身份认证），默认 "unknown"。
    note 截断到 256 字符进 meta.note。重复 ACK idempotent（每次刷新 ack_at + outbox）。
    """
    body = payload or AlarmAckRequest()
    result = await ack_alarm_event(
        alarm_id=alarm_id,
        operator=body.operator,
        note=body.note,
    )
    if result.get("action") == "not_found":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alarm not found")
    if result.get("action") == "error":
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="alarm ack failed")
    return {"success": True, **result}


@router.get("/history")
async def get_alarm_history(
    period: str = Query("24h", description="时间范围，例如 24h / 7d"),
    severity: Optional[str] = Query(None, description="筛选 severity：warning/critical"),
    db: AsyncSession = Depends(get_db),
):
    """
    获取历史报警（含已恢复，按 raised_at >= now - period 过滤）。
    """
    try:
        since = _parse_period(period)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    where_clauses: list = [AlarmEvent.raised_at >= since]
    if severity:
        where_clauses.append(AlarmEvent.severity == severity)
    stmt = (
        select(AlarmEvent)
        .where(*where_clauses)
        .order_by(AlarmEvent.raised_at.desc(), AlarmEvent.id.desc())
    )
    rs = await db.execute(stmt)
    rows = list(rs.scalars().all())
    return {
        "success": True,
        "count": len(rows),
        "data": [_serialize_alarm(r) for r in rows],
        "timestamp": int(time.time()),
    }


class AlarmIngestRequest(BaseModel):
    alarm_key: str = Field(..., min_length=1, max_length=128)
    source_service: str = Field(..., min_length=1, max_length=64)
    state: str = Field(..., description='"firing" 或 "resolved"')
    severity: Optional[str] = Field(None, max_length=16)
    tenant_id: Optional[str] = Field(None, max_length=64)
    line_id: Optional[str] = Field(None, max_length=32)
    node_id: Optional[str] = Field(None, max_length=128)
    node_name: Optional[str] = Field(None, max_length=128)
    device_id: Optional[str] = Field(None, max_length=128)
    message: Optional[str] = None
    occurred_at: Optional[dt.datetime] = None
    meta: dict[str, Any] = Field(default_factory=dict)


@router.post("/internal/ingest")
async def ingest_alarm_transition(
    payload: AlarmIngestRequest,
    _: str = Depends(require_admin_api_key),
):
    """
    内部端点：供 edge-monitor 上报状态转移。

    state="firing" / "resolved"，按 (tenant_id, line_id, alarm_key) 去重：
    open firing 命中则 update（last_seen/occurrence_count/severity），否则 insert；
    resolved 命中 open 行则 update resolved_at，否则 no-op。
    """
    try:
        result = await apply_alarm_transition(
            alarm_key=payload.alarm_key,
            source_service=payload.source_service,
            state=payload.state,
            severity=payload.severity,
            tenant_id=payload.tenant_id,
            line_id=payload.line_id,
            node_id=payload.node_id,
            node_name=payload.node_name,
            device_id=payload.device_id,
            message=payload.message,
            occurred_at=payload.occurred_at,
            meta=dict(payload.meta or {}),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {"success": True, **result}
