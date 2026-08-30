from __future__ import annotations

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

    def one_or_none(self):
        return self._rows[0] if self._rows else None


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

    async def execute(self, _stmt):
        return _FakeExecuteResult(self.rows)

    async def flush(self):
        if self.added and getattr(self.added[0], "id", None) is None:
            self.added[0].id = 8

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_record_ota_operation_adds_operation_and_outbox(monkeypatch):
    from app.models import CloudSyncOutbox, OtaOperation
    from app.services import ota_history as oh_module

    session = _FakeSession()
    monkeypatch.setattr(oh_module, "AsyncSessionLocal", lambda: session)
    oh_module.settings.CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS = 10

    await oh_module.record_ota_operation(
        {
            "operation_id": "ota-1",
            "status": "queued",
            "mode": "online",
            "target_version": "2026.05.22",
            "started_at": None,
        },
        request_payload={"mode": "online", "target_version": "2026.05.22"},
    )

    assert session.committed is True
    assert session.rolled_back is False
    assert len(session.added) == 2
    assert isinstance(session.added[0], OtaOperation)
    assert isinstance(session.added[1], CloudSyncOutbox)

    row = session.added[0]
    assert row.operation_id == "ota-1"
    assert row.ota_version == "2026.05.22"
    assert row.source == "online"
    assert row.status == "queued"

    outbox = session.added[1]
    assert outbox.kind == "ota_operation"
    assert outbox.max_attempts == 10
    assert outbox.payload["edge_ota_id"] == 8
    assert outbox.payload["operation_id"] == "ota-1"
    assert outbox.payload["ota_version"] == "2026.05.22"
    assert outbox.payload["status"] == "queued"
    assert outbox.payload["params"]["request"]["target_version"] == "2026.05.22"


@pytest.mark.asyncio
async def test_record_ota_operation_uses_signed_release_receipt_when_version_is_absent(monkeypatch):
    from app.services import ota_history as oh_module

    release_id = "22222222-2222-4222-8222-222222222222"
    manifest_digest = "sha256:" + "2" * 64
    session = _FakeSession()
    monkeypatch.setattr(oh_module, "AsyncSessionLocal", lambda: session)

    await oh_module.record_ota_operation(
        {
            "operation_id": "00000000000000020000000000000002",
            "status": "succeeded",
            "release_id": release_id,
            "manifest_digest": manifest_digest,
            "sequence": 2,
            "exit_code": 0,
        }
    )

    row = session.added[0]
    assert row.ota_version == release_id
    assert row.result["release_identity"] == {
        "release_id": release_id,
        "manifest_digest": manifest_digest,
        "sequence": 2,
    }
    assert session.added[1].payload["result"]["release_identity"] == row.result["release_identity"]


@pytest.mark.asyncio
async def test_record_ota_operation_updates_existing_and_writes_outbox(monkeypatch):
    from app.models import CloudSyncOutbox, OtaOperation
    from app.services import ota_history as oh_module

    existing = OtaOperation(
        id=9,
        operation_id="ota-1",
        ota_version="old",
        source="online",
        status="queued",
        started_at=dt.datetime(2026, 5, 22, 1, 0, tzinfo=dt.timezone.utc),
    )
    session = _FakeSession(rows=[existing])
    monkeypatch.setattr(oh_module, "AsyncSessionLocal", lambda: session)

    await oh_module.record_ota_operation(
        {
            "operation_id": "ota-1",
            "status": "succeeded",
            "mode": "online",
            "target_version": "2026.05.22",
            "finished_at": "2026-05-22T02:00:00+00:00",
            "exit_code": 0,
        }
    )

    assert session.committed is True
    assert session.added and isinstance(session.added[0], CloudSyncOutbox)
    assert existing.status == "succeeded"
    assert existing.finished_at == dt.datetime(2026, 5, 22, 2, 0, tzinfo=dt.timezone.utc)
    assert session.added[0].payload["edge_ota_id"] == 9
    assert session.added[0].payload["result"]["exit_code"] == 0


@pytest.mark.asyncio
async def test_record_ota_operation_does_not_enqueue_unchanged_poll(monkeypatch):
    from app.models import OtaOperation
    from app.services import ota_history as oh_module

    started_at = dt.datetime(2026, 5, 22, 1, 0, tzinfo=dt.timezone.utc)
    existing = OtaOperation(
        id=9,
        operation_id="ota-1",
        ota_version="2026.05.22",
        source="online",
        status="running",
        started_at=started_at,
        params={
            "operation_id": "ota-1",
            "request": {"mode": "online", "target_version": "2026.05.22"},
            "step": "apply",
            "timeout_sec": None,
        },
        log_path="/var/log/drycontroller-ota/operations.log",
        result={
            "exit_code": None,
            "error": None,
            "helper_container_id": None,
            "helper_image": None,
        },
    )
    session = _FakeSession(rows=[existing])
    monkeypatch.setattr(oh_module, "AsyncSessionLocal", lambda: session)

    await oh_module.record_ota_operation(
        {
            "operation_id": "ota-1",
            "status": "running",
            "mode": "online",
            "target_version": "2026.05.22",
            "started_at": started_at.isoformat(),
            "step": "apply",
        },
        request_payload={"mode": "online", "target_version": "2026.05.22"},
    )

    assert session.committed is True
    assert session.added == []


@pytest.mark.asyncio
async def test_record_ota_operation_does_not_overwrite_history_with_replay_rejection(monkeypatch):
    from app.services import ota_history as oh_module

    def unexpected_session():
        raise AssertionError("replay rejection must not touch OTA history")

    monkeypatch.setattr(oh_module, "AsyncSessionLocal", unexpected_session)

    await oh_module.record_ota_operation(
        {
            "operation_id": "ota-1",
            "status": "failed",
            "step": "retained",
            "error": "operation_replay_rejected",
            "log_available": False,
        }
    )


@pytest.mark.asyncio
async def test_outbox_drain_pushes_ota_operation_to_ota_path(monkeypatch):
    from app.models import CloudSyncOutbox
    from app.services import outbox_drain as drain_module
    from app.services.outbox_drain import CloudSyncOutboxDrainer

    monkeypatch.setattr(drain_module.settings, "CLOUD_OPS_BASE_URL", "https://cloud.example")
    monkeypatch.setattr(drain_module, "load_runtime_identity", lambda: {"runtime_credential": "tk-ota"})
    monkeypatch.setattr(drain_module.settings, "CLOUD_SYNC_OUTBOX_OTA_PATH", "/api/v1/ota/from-edge")

    captured: dict = {}

    class _DummyResp:
        status_code = 200
        text = ""

    class _DummyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = dict(headers)
            return _DummyResp()

    monkeypatch.setattr(drain_module.httpx, "AsyncClient", _DummyClient)

    row = CloudSyncOutbox(kind="ota_operation", payload={"edge_ota_id": 1, "operation_id": "ota-1"})
    drainer = CloudSyncOutboxDrainer()

    ok, err = await drainer._push("ota_operation", [row])

    assert ok is True
    assert err == ""
    assert captured["url"] == "https://cloud.example/api/v1/ota/from-edge"
    assert captured["json"] == {"items": [row.payload]}
    assert captured["headers"]["X-Device-Credential"] == "tk-ota"
