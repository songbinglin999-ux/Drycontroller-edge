from __future__ import annotations

import asyncio
import datetime as dt
import os
from typing import Any

import httpx
import structlog
from sqlalchemy import or_, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models import CloudSyncOutbox

logger = structlog.get_logger(__name__)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _retry_at(attempts: int) -> dt.datetime:
    delay = min(300, 2 ** max(0, attempts - 1))
    return _utcnow() + dt.timedelta(seconds=delay)


class CloudSyncOutboxDrainer:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        if not settings.CLOUD_SYNC_OUTBOX_ENABLED:
            logger.info("cloud_sync_outbox_disabled")
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop())
        logger.info("cloud_sync_outbox_drain_started")

    async def stop(self) -> None:
        if not self._task:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("cloud_sync_outbox_drain_stopped")

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("cloud_sync_outbox_drain_loop_failed", error=str(exc))
            await asyncio.sleep(max(0.2, float(settings.CLOUD_SYNC_OUTBOX_INTERVAL_SEC)))

    async def drain_once(self) -> int:
        now = _utcnow()
        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(CloudSyncOutbox)
                .where(
                    CloudSyncOutbox.synced_at.is_(None),
                    CloudSyncOutbox.status == "pending",
                    or_(CloudSyncOutbox.next_retry_at.is_(None), CloudSyncOutbox.next_retry_at <= now),
                )
                .order_by(CloudSyncOutbox.created_at.asc(), CloudSyncOutbox.id.asc())
                .limit(max(1, int(settings.CLOUD_SYNC_OUTBOX_BATCH_SIZE)))
            )
            rows = list(res.scalars().all())

            pushed = 0
            for row in rows:
                ok, error = await self._push(row.kind, row.payload)
                now = _utcnow()
                row.last_attempt_at = now
                if ok:
                    row.status = "synced"
                    row.synced_at = now
                    row.last_error = None
                    pushed += 1
                    continue

                row.attempts = int(row.attempts or 0) + 1
                row.last_error = error[:4000]
                max_attempts = row.max_attempts if row.max_attempts is not None else settings.CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS
                if max_attempts is not None and row.attempts >= int(max_attempts):
                    row.status = "dead"
                    row.next_retry_at = None
                else:
                    row.next_retry_at = _retry_at(row.attempts)

            await db.commit()
            return pushed

    async def _push(self, kind: str, payload: dict[str, Any]) -> tuple[bool, str]:
        if kind != "audit":
            return False, f"unsupported_outbox_kind:{kind}"

        base_url = os.getenv("CLOUD_OPS_BASE_URL", "").strip()
        enroll_token = os.getenv("CLOUD_OPS_ENROLL_TOKEN", "").strip()
        token_header = os.getenv("CLOUD_OPS_ENROLL_TOKEN_HEADER", "X-Enroll-Token").strip() or "X-Enroll-Token"
        if not base_url or not enroll_token:
            return False, "missing_cloud_ops_config"

        url = f"{base_url.rstrip('/')}{settings.CLOUD_SYNC_OUTBOX_AUDIT_PATH}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload, headers={token_header: enroll_token})
            if 200 <= resp.status_code < 300:
                return True, ""
            body = resp.text[:1000] if resp.text else ""
            return False, f"http_{resp.status_code}:{body}"
        except Exception as exc:
            return False, str(exc)


cloud_sync_outbox_drainer = CloudSyncOutboxDrainer()
