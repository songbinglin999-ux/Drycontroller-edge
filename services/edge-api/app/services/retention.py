from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass

import structlog
from sqlalchemy import text

from app.core.config import settings
from app.core.database import AsyncSessionLocal


logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RetentionPolicy:
    table: str
    timestamp_column: str
    retention_days: int
    extra_where: str = ""


EDGE_RETENTION_POLICIES: tuple[RetentionPolicy, ...] = (
    RetentionPolicy("operation_audit_logs", "created_at", 365),
    RetentionPolicy("control_command_history", "requested_at", 365),
    RetentionPolicy("cloud_sync_outbox", "synced_at", 7, "synced_at IS NOT NULL"),
)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _where_clause(policy: RetentionPolicy) -> str:
    clause = f"{policy.timestamp_column} < :cutoff"
    if policy.extra_where:
        clause = f"({clause}) AND ({policy.extra_where})"
    return clause


async def _count_expired(session, policy: RetentionPolicy, cutoff: dt.datetime) -> int:
    result = await session.execute(
        text(f"SELECT COUNT(1) FROM {policy.table} WHERE {_where_clause(policy)}"),
        {"cutoff": cutoff},
    )
    return int(result.scalar_one() or 0)


async def _delete_expired_batch(session, policy: RetentionPolicy, cutoff: dt.datetime, batch_size: int) -> int:
    result = await session.execute(
        text(
            f"""
            DELETE FROM {policy.table}
            WHERE id IN (
                SELECT id
                FROM {policy.table}
                WHERE {_where_clause(policy)}
                ORDER BY id
                LIMIT :limit
            )
            """
        ),
        {"cutoff": cutoff, "limit": batch_size},
    )
    rowcount = int(result.rowcount or 0)
    return max(rowcount, 0)


async def run_retention_once(
    *,
    now: dt.datetime | None = None,
    dry_run: bool = False,
    batch_size: int | None = None,
) -> dict[str, int]:
    batch_limit = max(1, int(batch_size or settings.DATA_RETENTION_BATCH_SIZE))
    current_time = now or _utcnow()
    totals: dict[str, int] = {}

    async with AsyncSessionLocal() as session:
        for policy in EDGE_RETENTION_POLICIES:
            cutoff = current_time - dt.timedelta(days=policy.retention_days)
            if dry_run:
                totals[policy.table] = await _count_expired(session, policy, cutoff)
                continue

            deleted = 0
            while True:
                batch_deleted = await _delete_expired_batch(session, policy, cutoff, batch_limit)
                await session.commit()
                deleted += batch_deleted
                if batch_deleted < batch_limit:
                    break
            totals[policy.table] = deleted

    return totals


class RetentionRunner:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        if not settings.DATA_RETENTION_ENABLED:
            logger.info("data_retention_disabled")
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop())
        logger.info("data_retention_started", interval_sec=settings.DATA_RETENTION_INTERVAL_SEC)

    async def stop(self) -> None:
        if not self._task:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("data_retention_stopped")

    async def _loop(self) -> None:
        interval = max(60.0, float(settings.DATA_RETENTION_INTERVAL_SEC))
        while not self._stop.is_set():
            try:
                totals = await run_retention_once()
                logger.info("data_retention_completed", totals=totals)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 故意宽捕：保留清理后台环韧性边界，未知异常也须记录并继续
                logger.error("data_retention_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass


retention_runner = RetentionRunner()
