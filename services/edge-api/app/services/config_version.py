from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

import structlog

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models import CloudSyncOutbox, ConfigVersion

logger = structlog.get_logger(__name__)


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).isoformat()


def _checksum(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def record_config_version(
    *,
    scope: str,
    payload: dict[str, Any],
    source: str,
    applied_by: str | None = None,
    restart_required: bool = False,
    active: bool = True,
    previous_version: str | None = None,
) -> None:
    """
    Persist a concrete applied config payload and enqueue it for cloud sync.

    The config mutation itself owns the business outcome. Version history is
    best-effort so a transient history failure does not make the mutation lie.
    """
    checksum = _checksum(payload)
    try:
        async with AsyncSessionLocal() as db:
            try:
                row = ConfigVersion(
                    scope=scope,
                    version=f"sha256:{checksum[:16]}",
                    checksum=checksum,
                    source=source,
                    payload=dict(payload or {}),
                    active=active,
                    previous_version=previous_version,
                    applied_by=applied_by,
                    restart_required=restart_required,
                )
                db.add(row)
                await db.flush()

                db.add(
                    CloudSyncOutbox(
                        kind="config_version",
                        max_attempts=settings.CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS,
                        payload={
                            "edge_config_id": row.id,
                            "scope": row.scope,
                            "version": row.version,
                            "checksum": row.checksum,
                            "source": row.source,
                            "payload": dict(row.payload or {}),
                            "active": row.active,
                            "previous_version": row.previous_version,
                            "applied_at": _iso(row.applied_at),
                            "applied_by": row.applied_by,
                            "restart_required": row.restart_required,
                            "rolled_back_at": _iso(row.rolled_back_at),
                        },
                    )
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    except Exception as exc:
        logger.error("edge_config_version_write_failed", scope=scope, error=str(exc))
