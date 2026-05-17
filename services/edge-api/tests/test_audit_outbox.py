import datetime as dt
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]
for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalarResult(self._rows)


class _FakeSession:
    def __init__(self, rows=None, fail_on_add=0):
        self.rows = rows or []
        self.fail_on_add = fail_on_add
        self.added = []
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def add(self, obj):
        self.added.append(obj)
        if self.fail_on_add and len(self.added) >= self.fail_on_add:
            raise RuntimeError("add failed")

    async def flush(self):
        self.added[0].id = 123

    async def execute(self, _stmt):
        return _FakeExecuteResult(self.rows)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_write_audit_adds_log_and_outbox_in_one_commit(monkeypatch):
    from app.models import CloudSyncOutbox, OperationAuditLog
    from app.services import audit as audit_module

    session = _FakeSession()
    monkeypatch.setattr(audit_module, "AsyncSessionLocal", lambda: session)
    audit_module.settings.CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS = 10

    await audit_module.write_audit(
        actor="admin:key:test",
        action="license.install",
        resource_type="license",
        resource_id="lic-1",
        result="success",
        detail={"machine_id": "m1"},
    )

    assert session.committed is True
    assert session.rolled_back is False
    assert len(session.added) == 2
    assert isinstance(session.added[0], OperationAuditLog)
    assert isinstance(session.added[1], CloudSyncOutbox)
    assert session.added[1].kind == "audit"
    assert session.added[1].max_attempts == 10
    assert session.added[1].payload["edge_audit_id"] == 123
    assert session.added[1].payload["action"] == "license.install"


@pytest.mark.asyncio
async def test_write_audit_rolls_back_when_outbox_insert_fails(monkeypatch):
    from app.services import audit as audit_module

    session = _FakeSession(fail_on_add=2)
    monkeypatch.setattr(audit_module, "AsyncSessionLocal", lambda: session)

    await audit_module.write_audit(actor="admin", action="license.install")

    assert session.committed is False
    assert session.rolled_back is True


@pytest.mark.asyncio
async def test_outbox_drain_success_marks_synced(monkeypatch):
    from app.models import CloudSyncOutbox
    from app.services.outbox_drain import CloudSyncOutboxDrainer
    from app.services import outbox_drain as drain_module

    row = CloudSyncOutbox(kind="audit", payload={"action": "license.install"}, max_attempts=10)
    row.id = 1
    row.status = "pending"
    row.attempts = 0
    row.created_at = dt.datetime.now(dt.timezone.utc)
    row.synced_at = None
    row.next_retry_at = None
    session = _FakeSession(rows=[row])

    monkeypatch.setattr(drain_module, "AsyncSessionLocal", lambda: session)

    drainer = CloudSyncOutboxDrainer()

    async def fake_push(kind, payload):
        return True, ""

    monkeypatch.setattr(drainer, "_push", fake_push)

    pushed = await drainer.drain_once()

    assert pushed == 1
    assert row.status == "synced"
    assert row.synced_at is not None
    assert session.committed is True


@pytest.mark.asyncio
async def test_outbox_drain_failure_retries_then_dead(monkeypatch):
    from app.models import CloudSyncOutbox
    from app.services.outbox_drain import CloudSyncOutboxDrainer
    from app.services import outbox_drain as drain_module

    row = CloudSyncOutbox(kind="audit", payload={"action": "license.install"}, max_attempts=2)
    row.id = 1
    row.status = "pending"
    row.attempts = 1
    row.created_at = dt.datetime.now(dt.timezone.utc)
    row.synced_at = None
    row.next_retry_at = None
    session = _FakeSession(rows=[row])

    monkeypatch.setattr(drain_module, "AsyncSessionLocal", lambda: session)

    drainer = CloudSyncOutboxDrainer()

    async def fake_push(kind, payload):
        return False, "http_500"

    monkeypatch.setattr(drainer, "_push", fake_push)

    pushed = await drainer.drain_once()

    assert pushed == 0
    assert row.attempts == 2
    assert row.status == "dead"
    assert row.synced_at is None
    assert row.last_error == "http_500"
