from __future__ import annotations

import hashlib
import time
from typing import Any

import structlog

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models import CloudSyncOutbox, OperationAuditLog

logger = structlog.get_logger(__name__)


def api_key_actor(api_key: str | None, *, role: str) -> str:
    if not api_key:
        return role
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
    return f"{role}:key:{digest}"


async def write_audit(
    *,
    actor: str,
    action: str,
    tenant_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    correlation_id: str | None = None,
    cloud_job_id: str | None = None,
    result: str | None = None,
    error_msg: str | None = None,
    detail: dict[str, Any] | None = None,
) -> int | None:
    """
    Append an edge audit event locally and enqueue the same event for cloud sync.

    Audit failures are swallowed after logging and reported as ``None``. Most
    callers keep audit best-effort; credential refresh uses the returned row ID
    as a required receipt before it mutates root-owned authentication state.
    """
    safe_actor = actor or "system"
    safe_detail = detail or {}
    try:
        async with AsyncSessionLocal() as db:
            try:
                log = OperationAuditLog(
                    tenant_id=tenant_id,
                    actor=safe_actor,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    correlation_id=correlation_id,
                    cloud_job_id=cloud_job_id,
                    result=result,
                    error_msg=error_msg,
                    detail=safe_detail,
                )
                db.add(log)
                await db.flush()

                db.add(
                    CloudSyncOutbox(
                        kind="audit",
                        max_attempts=settings.CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS,
                        payload={
                            "edge_audit_id": log.id,
                            "tenant_id": tenant_id,
                            "actor": safe_actor,
                            "action": action,
                            "target": _target(resource_type, resource_id),
                            "resource_type": resource_type,
                            "resource_id": resource_id,
                            "correlation_id": correlation_id,
                            "cloud_job_id": cloud_job_id,
                            "result": result,
                            "error_msg": error_msg,
                            "detail": safe_detail,
                            "edge_created_at": int(time.time()),
                        },
                    )
                )
                await db.commit()
                return log.id
            except Exception:  # noqa: BLE001 - 故意宽捕：best-effort audit writer 失败需 rollback 后交给外层吞掉
                await db.rollback()
                raise
    except Exception as exc:  # noqa: BLE001 - 故意宽捕：审计写入失败不能改变业务操作结果
        logger.error("edge_audit_write_failed", action=action, error=str(exc))
        return None


def _target(resource_type: str | None, resource_id: str | None) -> str | None:
    if resource_type and resource_id:
        return f"{resource_type}:{resource_id}"
    return resource_id or resource_type
