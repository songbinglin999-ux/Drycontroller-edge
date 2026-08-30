from __future__ import annotations

import datetime as dt
import re
from typing import Any
import uuid

import structlog
from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models import CloudSyncOutbox, OtaOperation

logger = structlog.get_logger(__name__)
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _parse_dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).isoformat()


def _operation_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("operation_id")
    return str(value).strip() if value else None


def _release_identity(operation: dict[str, Any]) -> dict[str, Any] | None:
    release_id = operation.get("release_id")
    manifest_digest = operation.get("manifest_digest")
    sequence = operation.get("sequence")
    try:
        canonical_release_id = str(uuid.UUID(str(release_id)))
    except ValueError:
        return None
    if (
        canonical_release_id != release_id
        or not isinstance(manifest_digest, str)
        or DIGEST.fullmatch(manifest_digest) is None
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 1
    ):
        return None
    return {
        "release_id": canonical_release_id,
        "manifest_digest": manifest_digest,
        "sequence": sequence,
    }


def _build_outbox_payload(row: OtaOperation) -> dict[str, Any]:
    return {
        "edge_ota_id": row.id,
        "tenant_id": row.tenant_id,
        "operation_id": row.operation_id,
        "ota_version": row.ota_version,
        "source": row.source,
        "status": row.status,
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "rollback_at": _iso(row.rollback_at),
        "params": dict(row.params or {}),
        "log_excerpt": row.log_excerpt,
        "log_path": row.log_path,
        "result": dict(row.result or {}),
    }


async def record_ota_operation(
    operation: dict[str, Any],
    *,
    request_payload: dict[str, Any] | None = None,
    tenant_id: str | None = None,
) -> None:
    """
    Mirror ota-agent operation state into edge DB and enqueue it for cloud sync.

    OTA execution must not fail because history persistence failed, so this
    follows the same best-effort contract as audit/control outbox writers.
    """
    op_id = _operation_id(operation)
    if not op_id:
        logger.error("edge_ota_history_missing_operation_id")
        return
    if operation.get("error") == "operation_replay_rejected":
        logger.warning("edge_ota_history_replay_rejection_not_mirrored", operation_id=op_id)
        return

    req = request_payload or {}
    status_text = str(operation.get("status") or "unknown")
    parsed_started_at = _parse_dt(operation.get("started_at"))
    finished_at = _parse_dt(operation.get("finished_at"))
    release_identity = _release_identity(operation)

    try:
        async with AsyncSessionLocal() as db:
            try:
                res = await db.execute(select(OtaOperation).where(OtaOperation.operation_id == op_id).limit(1))
                row = res.scalars().one_or_none()
                previous_payload = _build_outbox_payload(row) if row is not None else None
                stored_request = (
                    dict((row.params or {}).get("request") or {})
                    if row is not None and isinstance(row.params, dict)
                    else {}
                )
                stored_result = (
                    dict(row.result or {})
                    if row is not None and isinstance(row.result, dict)
                    else {}
                )
                stored_release_identity = _release_identity(
                    dict(stored_result.get("release_identity") or {})
                )
                effective_release_identity = stored_release_identity or release_identity
                effective_req = req if request_payload is not None else stored_request
                receipt_label = str(
                    effective_req.get("target_version")
                    or (effective_release_identity or {}).get("release_id")
                    or op_id
                )
                if row is None:
                    row = OtaOperation(
                        tenant_id=tenant_id,
                        operation_id=op_id,
                        ota_version=receipt_label,
                        source=str(operation.get("mode") or effective_req.get("mode") or "online"),
                        status=status_text,
                        started_at=parsed_started_at or dt.datetime.now(dt.timezone.utc),
                    )
                    db.add(row)

                row.status = status_text
                row.ota_version = str(
                    effective_req.get("target_version")
                    or (effective_release_identity or {}).get("release_id")
                    or row.ota_version
                    or op_id
                )
                row.source = str(operation.get("mode") or effective_req.get("mode") or row.source or "online")
                if parsed_started_at is not None:
                    row.started_at = parsed_started_at
                row.finished_at = finished_at
                row.params = {
                    "operation_id": op_id,
                    "request": dict(effective_req),
                    "step": operation.get("step"),
                    "timeout_sec": operation.get("timeout_sec") or effective_req.get("timeout_sec"),
                }
                row.log_path = "/var/log/drycontroller-ota/operations.log"
                row.result = {
                    "exit_code": operation.get("exit_code"),
                    "error": operation.get("error"),
                    "helper_container_id": operation.get("helper_container_id"),
                    "helper_image": operation.get("helper_image"),
                }
                if effective_release_identity is not None:
                    row.result["release_identity"] = effective_release_identity
                await db.flush()

                current_payload = _build_outbox_payload(row)
                if current_payload != previous_payload:
                    db.add(
                        CloudSyncOutbox(
                            kind="ota_operation",
                            max_attempts=settings.CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS,
                            payload=current_payload,
                        )
                    )
                await db.commit()
            except Exception:  # noqa: BLE001 - 故意宽捕：best-effort OTA history 失败需 rollback 后交给外层吞掉
                await db.rollback()
                raise
    except Exception as exc:  # noqa: BLE001 - 故意宽捕：OTA 执行结果不能被历史持久化失败改写
        logger.error("edge_ota_history_write_failed", operation_id=op_id, error=str(exc))
