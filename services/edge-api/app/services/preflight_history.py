from __future__ import annotations

import json
from typing import Any

import structlog

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models import CloudSyncOutbox, PreflightRun
from app.services.edge_identity import load_edge_identity

logger = structlog.get_logger(__name__)


async def record_preflight_run(payload: dict[str, Any]) -> None:
    """
    Persist an edge preflight result locally and enqueue it for cloud sync.

    Preflight must return the observed result even when history persistence is
    unavailable, so failures are logged and swallowed like other outbox writers.
    """
    status = str(payload.get("status") or "fail")
    summary = payload.get("summary")
    summary_text = json.dumps(summary, ensure_ascii=False, sort_keys=True) if summary is not None else None

    try:
        async with AsyncSessionLocal() as db:
            try:
                row = PreflightRun(
                    status=status,
                    ok=status == "pass",
                    summary=summary_text,
                    json_output=dict(payload or {}),
                )
                db.add(row)
                await db.flush()
                identity = load_edge_identity()

                db.add(
                    CloudSyncOutbox(
                        kind="preflight_run",
                        max_attempts=settings.CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS,
                        payload={
                            "edge_preflight_id": row.id,
                            **identity,
                            "run_at": row.run_at.isoformat() if row.run_at else None,
                            "status": row.status,
                            "ok": row.ok,
                            "summary": row.summary,
                            "json_output": dict(row.json_output or {}),
                        },
                    )
                )
                await db.commit()
            except Exception:  # noqa: BLE001 - 故意宽捕：best-effort preflight history 失败需 rollback 后交给外层吞掉
                await db.rollback()
                raise
    except Exception as exc:  # noqa: BLE001 - 故意宽捕：preflight 结果不能被历史持久化失败改写
        logger.error("edge_preflight_history_write_failed", status=status, error=str(exc))
