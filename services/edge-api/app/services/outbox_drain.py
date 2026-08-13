from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
import structlog
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.device_identity import DeviceIdentityUnavailable, load_runtime_identity
from app.models import AlarmEvent, CloudSyncOutbox
from app.services.alarm_event import build_alarm_outbox_payload

logger = structlog.get_logger(__name__)

# 多 worker 部署时只允许一个 drainer 跑（与 cloud-ops NodeHealthPoller P1-N4 同款 pattern）
_PG_ADVISORY_LOCK_KEY = 0x4452434F5554424F  # "DRCOUTBO"
_IDENTITY_ALARM_SCHEMA = "drycontroller.device-identity-alarm.v1"
_EVENT_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_IDENTITY_ALARM_FILENAME_RE = re.compile(r"^[0-9]{20}-[a-f0-9]{32}\.json$")
_IDENTITY_ALARM_FIELDS = {"schema", "event_id", "episode_id", "state", "device_id", "occurred_at"}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _retry_at(attempts: int) -> dt.datetime:
    delay = min(300, 2 ** min(16, max(0, attempts - 1)))
    return _utcnow() + dt.timedelta(seconds=delay)


def _is_permanent_client_error(error: str) -> bool:
    """
    永久失败 = payload 被云端明确拒绝（重试无效）。
    网络异常 / 5xx / 配置缺失等瞬态失败不算：outbox 的意义就是断连后最终一致，
    现场长断网恢复后必须能把积压数据补传上去。
    """
    if not error.startswith("http_"):
        return False
    try:
        status_code = int(error.removeprefix("http_").split(":", 1)[0])
    except ValueError:
        return False
    return status_code in {400, 409, 413, 422}


def _should_split_batch_error(error: str) -> bool:
    # 可拆批隔离的错误 = 同一集合的永久业务拒绝（隔离批内毒丸行）
    return _is_permanent_client_error(error)


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
            except Exception as exc:  # noqa: BLE001 - 故意宽捕：云同步积压重试环韧性边界，断网/未知异常也须记录并继续
                logger.error("cloud_sync_outbox_drain_loop_failed", error=str(exc))
            await asyncio.sleep(max(0.2, float(settings.CLOUD_SYNC_OUTBOX_INTERVAL_SEC)))

    async def drain_once(self) -> int:
        async with AsyncSessionLocal() as db:
            if not await self._try_acquire_drain_lock(db):
                logger.debug("cloud_sync_outbox_drain_lock_busy")
                return 0
            try:
                return await self._drain_locked(db)
            finally:
                await self._release_drain_lock(db)

    async def _drain_locked(self, db: AsyncSession) -> int:
        alarm_paths = await self._ingest_identity_alarm_marker(db)
        # Alarm transitions must be durable before any Cloud side effect. A
        # crash after Cloud accepts a push can then retry the same edge_alarm_id.
        await db.commit()
        self._compact_identity_alarm_queue(alarm_paths)
        now = _utcnow()
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
        if not rows:
            return 0

        by_kind: dict[str, list[CloudSyncOutbox]] = defaultdict(list)
        for row in rows:
            by_kind[row.kind].append(row)

        pushed = 0
        for kind, kind_rows in by_kind.items():
            pushed += await self._push_kind_rows(kind, kind_rows)

        await db.commit()
        return pushed

    async def _ingest_identity_alarm_marker(self, db: AsyncSession) -> list[Path]:
        try:
            alarm_dir = Path(settings.DEVICE_IDENTITY_PATH).with_name("device-identity-alarms")
            paths = sorted(alarm_dir.glob("*.json"))
        except OSError:
            logger.error("device_identity_alarm_queue_unavailable")
            return []
        if not paths:
            return []
        if len(paths) > 4096:
            logger.error("device_identity_alarm_queue_too_large", count=len(paths))
            return []
        events: list[tuple[dict[str, str], dt.datetime]] = []
        for path in paths:
            try:
                marker = json.loads(path.read_text(encoding="utf-8"))
                if (
                    not _IDENTITY_ALARM_FILENAME_RE.fullmatch(path.name)
                    or not isinstance(marker, dict)
                    or set(marker) != _IDENTITY_ALARM_FIELDS
                    or marker.get("schema") != _IDENTITY_ALARM_SCHEMA
                    or not _EVENT_ID_RE.fullmatch(str(marker.get("event_id") or ""))
                    or not _EVENT_ID_RE.fullmatch(str(marker.get("episode_id") or ""))
                    or marker.get("event_id") not in path.name
                    or marker.get("state") not in {"firing", "resolved"}
                    or not isinstance(marker.get("device_id"), str)
                ):
                    raise ValueError("invalid identity alarm event")
                occurred_at = dt.datetime.fromisoformat(str(marker["occurred_at"]).replace("Z", "+00:00"))
                if occurred_at.tzinfo is None:
                    occurred_at = occurred_at.replace(tzinfo=dt.timezone.utc)
                events.append((marker, occurred_at.astimezone(dt.timezone.utc)))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                logger.error("device_identity_alarm_event_invalid", file=path.name)
                return []

        rows_by_device: dict[str, list[AlarmEvent]] = {}
        for marker, occurred_at in events:
            device_id = marker["device_id"]
            if device_id not in rows_by_device:
                result = await db.execute(
                    select(AlarmEvent)
                    .where(
                        AlarmEvent.alarm_key == "device.identity.credential_rotation",
                        AlarmEvent.device_id == device_id,
                    )
                    .order_by(AlarmEvent.raised_at.asc(), AlarmEvent.id.asc())
                )
                rows_by_device[device_id] = list(result.scalars().all())
            rows = rows_by_device[device_id]
            if any(marker["event_id"] in (row.meta or {}).get("event_ids", []) for row in rows):
                continue
            current = next(
                (
                    row
                    for row in reversed(rows)
                    if row.resolved_at is None and (row.meta or {}).get("episode_id") == marker["episode_id"]
                ),
                None,
            )
            if marker["state"] == "firing":
                if current is None:
                    current = AlarmEvent(
                        device_id=device_id,
                        node_id=device_id,
                        severity="critical",
                        source_service="device-identity",
                        alarm_key="device.identity.credential_rotation",
                        message="Device credential rotation failed",
                        state="firing",
                        raised_at=occurred_at,
                        first_seen_at=occurred_at,
                        last_seen_at=occurred_at,
                        occurrence_count=1,
                        meta={"episode_id": marker["episode_id"], "event_ids": [marker["event_id"]]},
                    )
                    db.add(current)
                    await db.flush()
                    rows.append(current)
                else:
                    event_ids = list((current.meta or {}).get("event_ids", []))
                    event_ids.append(marker["event_id"])
                    current.meta = {"episode_id": marker["episode_id"], "event_ids": event_ids}
                    current.last_seen_at = occurred_at
                    current.occurrence_count = int(current.occurrence_count or 0) + 1
            elif current is not None:
                event_ids = list((current.meta or {}).get("event_ids", []))
                event_ids.append(marker["event_id"])
                current.meta = {"episode_id": marker["episode_id"], "event_ids": event_ids}
                current.state = "resolved"
                current.last_seen_at = occurred_at
                current.resolved_at = occurred_at
            else:
                logger.error("device_identity_alarm_resolution_without_firing", event_id=marker["event_id"])
                return []
            db.add(
                CloudSyncOutbox(
                    kind="alarm",
                    max_attempts=settings.CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS,
                    payload=build_alarm_outbox_payload(current),
                )
            )
        return paths

    @staticmethod
    def _compact_identity_alarm_queue(paths: list[Path]) -> None:
        if len(paths) <= 1:
            return
        for path in paths[:-1]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.error("device_identity_alarm_event_cleanup_failed", file=path.name)
                return
        try:
            directory_fd = os.open(paths[-1].parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            logger.error("device_identity_alarm_queue_fsync_failed")

    async def _push_kind_rows(self, kind: str, rows: list[CloudSyncOutbox]) -> int:
        ok, error = await self._push(kind, rows)
        if ok:
            attempt_at = _utcnow()
            for row in rows:
                self._mark_synced(row, attempt_at)
            return len(rows)

        if len(rows) > 1 and _should_split_batch_error(error):
            logger.warning(
                "cloud_sync_outbox_batch_split",
                kind=kind,
                count=len(rows),
                error=error[:1000],
            )
            pushed = 0
            for row in rows:
                row_ok, row_error = await self._push(kind, [row])
                attempt_at = _utcnow()
                if row_ok:
                    self._mark_synced(row, attempt_at)
                    pushed += 1
                else:
                    self._mark_failed(row, row_error or error, attempt_at)
            return pushed

        attempt_at = _utcnow()
        for row in rows:
            self._mark_failed(row, error, attempt_at)
        return 0

    def _mark_synced(self, row: CloudSyncOutbox, attempt_at: dt.datetime) -> None:
        row.last_attempt_at = attempt_at
        row.status = "synced"
        row.synced_at = attempt_at
        row.last_error = None

    def _mark_failed(self, row: CloudSyncOutbox, error: str, attempt_at: dt.datetime) -> None:
        row.last_attempt_at = attempt_at
        row.attempts = int(row.attempts or 0) + 1
        row.last_error = error[:4000]
        # 只有永久失败（payload 被云端 4xx 明确拒绝）才计入 dead（毒丸防护）；
        # 瞬态失败（网络/5xx/云端未配置）无限退避重试，断网恢复后补传积压数据
        if _is_permanent_client_error(error):
            max_attempts = row.max_attempts if row.max_attempts is not None else settings.CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS
            if max_attempts is not None and row.attempts >= int(max_attempts):
                row.status = "dead"
                row.next_retry_at = None
                return
        row.next_retry_at = _retry_at(row.attempts)

    async def _try_acquire_drain_lock(self, session: AsyncSession) -> bool:
        if session.get_bind().dialect.name != "postgresql":
            return True
        res = await session.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": _PG_ADVISORY_LOCK_KEY})
        return bool(res.scalar())

    async def _release_drain_lock(self, session: AsyncSession) -> None:
        if session.get_bind().dialect.name != "postgresql":
            return
        await session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _PG_ADVISORY_LOCK_KEY})

    async def _push(self, kind: str, rows: list[CloudSyncOutbox]) -> tuple[bool, str]:
        if kind == "audit":
            path = settings.CLOUD_SYNC_OUTBOX_AUDIT_PATH
        elif kind == "control_command":
            path = settings.CLOUD_SYNC_OUTBOX_CONTROL_PATH
        elif kind == "alarm":
            path = settings.CLOUD_SYNC_OUTBOX_ALARM_PATH
        elif kind == "ota_operation":
            path = settings.CLOUD_SYNC_OUTBOX_OTA_PATH
        elif kind == "preflight_run":
            path = settings.CLOUD_SYNC_OUTBOX_PREFLIGHT_PATH
        elif kind == "config_version":
            path = settings.CLOUD_SYNC_OUTBOX_CONFIG_PATH
        else:
            return False, f"unsupported_outbox_kind:{kind}"

        base_url = (settings.CLOUD_OPS_BASE_URL or "").strip()
        try:
            identity = load_runtime_identity()
        except DeviceIdentityUnavailable:
            return False, "missing_device_identity"
        if not base_url:
            return False, "missing_cloud_ops_config"

        url = f"{base_url.rstrip('/')}{path}"
        body: dict[str, Any] = {"items": [row.payload for row in rows]}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    url,
                    json=body,
                    headers={"X-Device-Credential": identity["runtime_credential"]},
                )
            if 200 <= resp.status_code < 300:
                return True, ""
            text_body = resp.text[:1000] if resp.text else ""
            return False, f"http_{resp.status_code}:{text_body}"
        except httpx.HTTPError as exc:
            return False, str(exc)


cloud_sync_outbox_drainer = CloudSyncOutboxDrainer()
