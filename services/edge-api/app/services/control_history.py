from __future__ import annotations

import datetime as dt
from typing import Any

import structlog

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models import CloudSyncOutbox, ControlCommandHistory

logger = structlog.get_logger(__name__)


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).isoformat()


def _stringify_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


async def write_control_history(
    *,
    command_type: str,
    tag: str | None = None,
    value: Any = None,
    previous_value: Any = None,
    plc_readback: dict[str, Any] | None = None,
    line_id: str | None = None,
    tenant_id: str | None = None,
    node_id: str | None = None,
    cloud_job_id: str | None = None,
    correlation_id: str | None = None,
    idempotency_key: str | None = None,
    request_payload: dict[str, Any] | None = None,
    requested_by: str | None = None,
    ack_status: str = "pending",
    ack_at: dt.datetime | None = None,
    ack_detail: dict[str, Any] | None = None,
    error_msg: str | None = None,
) -> None:
    """
    Append a control command history row locally and enqueue the same event for
    cloud sync. Mirrors `write_audit`: failures are swallowed after logging so
    control responses are never blocked by history persistence issues.
    """
    safe_request_payload = request_payload or {}
    safe_ack_detail = ack_detail or {}
    value_text = _stringify_value(value)
    previous_value_text = _stringify_value(previous_value)
    safe_plc_readback = plc_readback or {}
    requested_at = dt.datetime.now(dt.timezone.utc)

    try:
        async with AsyncSessionLocal() as db:
            try:
                row = ControlCommandHistory(
                    tenant_id=tenant_id,
                    line_id=line_id,
                    cloud_job_id=cloud_job_id,
                    node_id=node_id,
                    command_type=command_type,
                    tag=tag,
                    value=value_text,
                    previous_value=previous_value_text,
                    plc_readback=safe_plc_readback,
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key,
                    request_payload=safe_request_payload,
                    requested_by=requested_by,
                    requested_at=requested_at,
                    ack_status=ack_status,
                    ack_at=ack_at,
                    ack_detail=safe_ack_detail,
                    error_msg=error_msg,
                )
                db.add(row)
                await db.flush()

                db.add(
                    CloudSyncOutbox(
                        kind="control_command",
                        max_attempts=settings.CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS,
                        payload={
                            "edge_control_id": row.id,
                            "tenant_id": tenant_id,
                            "line_id": line_id,
                            "node_id": node_id,
                            "cloud_job_id": cloud_job_id,
                            "command_type": command_type,
                            "tag": tag,
                            "value": value_text,
                            "previous_value": previous_value_text,
                            "plc_readback": safe_plc_readback,
                            "correlation_id": correlation_id,
                            "idempotency_key": idempotency_key,
                            "request_payload": safe_request_payload,
                            "requested_by": requested_by,
                            "requested_at": _iso(requested_at),
                            "ack_status": ack_status,
                            "ack_at": _iso(ack_at),
                            "ack_detail": safe_ack_detail,
                            "error_msg": error_msg,
                        },
                    )
                )
                await db.commit()
            except Exception:  # noqa: BLE001 - 故意宽捕：best-effort history writer 失败需 rollback 后交给外层吞掉
                await db.rollback()
                raise
    except Exception as exc:  # noqa: BLE001 - 故意宽捕：控制响应不能被历史持久化失败阻塞
        logger.error(
            "edge_control_history_write_failed",
            command_type=command_type,
            tag=tag,
            ack_status=ack_status,
            error=str(exc),
        )
