from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace

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


@pytest.mark.asyncio
async def test_identity_rotation_alarm_queue_replays_failure_then_recovery_after_drainer_restart(
    tmp_path, monkeypatch
):
    from app.models import AlarmEvent, CloudSyncOutbox
    from app.services import outbox_drain as drain_module

    alarm_dir = tmp_path / "device-identity-alarms"
    alarm_dir.mkdir()
    episode_id = "ef" * 16
    events = (
        ("00000000000000000001", "ab" * 16, "firing", "2026-08-13T01:02:03Z"),
        ("00000000000000000002", "cd" * 16, "resolved", "2026-08-13T01:03:03Z"),
    )
    for sequence, event_id, state, occurred_at in events:
        (alarm_dir / f"{sequence}-{event_id}.json").write_text(
            json.dumps(
                {
                    "schema": "drycontroller.device-identity-alarm.v1",
                    "event_id": event_id,
                    "episode_id": episode_id,
                    "state": state,
                    "device_id": "EDGE-ABCDEF123456",
                    "occurred_at": occurred_at,
                }
            ),
            encoding="utf-8",
        )
    partial = alarm_dir / ".00000000000000000003-partial.tmp"
    partial.write_text("{", encoding="utf-8")
    monkeypatch.setattr(drain_module.settings, "DEVICE_IDENTITY_PATH", str(tmp_path / "device.json"))
    session = _FakeSession(rows=[])

    drainer = drain_module.CloudSyncOutboxDrainer()
    processed = await drainer._ingest_identity_alarm_marker(session)

    assert len(session.added) == 3
    assert isinstance(session.added[0], AlarmEvent)
    assert isinstance(session.added[1], CloudSyncOutbox)
    assert isinstance(session.added[2], CloudSyncOutbox)
    event = session.added[0]
    assert event.id == 9
    assert event.state == "resolved"
    assert event.severity == "critical"
    assert event.device_id == "EDGE-ABCDEF123456"
    assert session.added[1].kind == "alarm"
    assert session.added[1].payload["edge_alarm_id"] == 9
    assert session.added[1].payload["state"] == "firing"
    assert session.added[2].payload["state"] == "resolved"
    assert event.meta["event_ids"] == ["ab" * 16, "cd" * 16]
    drainer._compact_identity_alarm_queue(processed)
    assert partial.exists()
    assert [path.name for path in alarm_dir.glob("*.json")] == [
        f"00000000000000000002-{'cd' * 16}.json"
    ]


@pytest.mark.asyncio
async def test_identity_rotation_alarm_queue_is_idempotent(tmp_path, monkeypatch):
    from app.models import AlarmEvent, CloudSyncOutbox
    from app.services import outbox_drain as drain_module

    raised = dt.datetime(2026, 8, 13, 1, 0, tzinfo=dt.timezone.utc)
    existing = AlarmEvent(
        device_id="EDGE-ABCDEF123456",
        node_id="EDGE-ABCDEF123456",
        severity="critical",
        source_service="device-identity",
        alarm_key="device.identity.credential_rotation",
        message="Device credential rotation failed",
        state="firing",
        raised_at=raised,
        first_seen_at=raised,
        last_seen_at=raised,
        occurrence_count=1,
        meta={"episode_id": "ef" * 16, "event_ids": ["ab" * 16]},
    )
    existing.id = 77
    existing.resolved_at = None
    alarm_dir = tmp_path / "device-identity-alarms"
    alarm_dir.mkdir()
    monkeypatch.setattr(drain_module.settings, "DEVICE_IDENTITY_PATH", str(tmp_path / "device.json"))
    session = _FakeSession(rows=[existing])
    drainer = drain_module.CloudSyncOutboxDrainer()

    (alarm_dir / f"00000000000000000001-{'ab' * 16}.json").write_text(
        json.dumps(
            {
                "schema": "drycontroller.device-identity-alarm.v1",
                "event_id": "ab" * 16,
                "episode_id": "ef" * 16,
                "state": "firing",
                "device_id": "EDGE-ABCDEF123456",
                "occurred_at": "2026-08-13T01:02:03Z",
            }
        ),
        encoding="utf-8",
    )
    await drainer._ingest_identity_alarm_marker(session)
    assert session.added == []

    (alarm_dir / f"00000000000000000002-{'cd' * 16}.json").write_text(
        json.dumps(
            {
                "schema": "drycontroller.device-identity-alarm.v1",
                "event_id": "cd" * 16,
                "episode_id": "ef" * 16,
                "state": "resolved",
                "device_id": "EDGE-ABCDEF123456",
                "occurred_at": "2026-08-13T01:03:03Z",
            }
        ),
        encoding="utf-8",
    )
    await drainer._ingest_identity_alarm_marker(session)
    assert existing.state == "resolved"
    assert existing.resolved_at == dt.datetime(2026, 8, 13, 1, 3, 3, tzinfo=dt.timezone.utc)
    assert len(session.added) == 1
    assert isinstance(session.added[0], CloudSyncOutbox)
    assert session.added[0].payload["state"] == "resolved"


class _FakeExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalarResult(self._rows)


class _FakeSession:
    def __init__(self, rows=None, fail_on_add=0, dialect="sqlite"):
        self.rows = rows or []
        self.fail_on_add = fail_on_add
        self.added = []
        self.committed = False
        self.commit_count = 0
        self.rolled_back = False
        self._bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get_bind(self):
        return self._bind

    def add(self, obj):
        self.added.append(obj)
        if self.fail_on_add and len(self.added) >= self.fail_on_add:
            raise RuntimeError("add failed")

    async def flush(self):
        self.added[0].id = 9

    async def execute(self, _stmt):
        return _FakeExecuteResult(self.rows)

    async def commit(self):
        self.committed = True
        self.commit_count += 1

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_outbox_drain_commits_local_alarm_state_before_cloud_push(tmp_path, monkeypatch):
    from app.models import CloudSyncOutbox
    from app.services import outbox_drain as drain_module

    row = CloudSyncOutbox(kind="alarm", payload={"edge_alarm_id": 9}, max_attempts=10)
    row.id = 1
    row.status = "pending"
    row.attempts = 0
    row.created_at = dt.datetime.now(dt.timezone.utc)
    row.synced_at = None
    row.next_retry_at = None
    session = _FakeSession(rows=[row])
    monkeypatch.setattr(drain_module.settings, "DEVICE_IDENTITY_PATH", str(tmp_path / "device.json"))
    drainer = drain_module.CloudSyncOutboxDrainer()

    async def fail_after_cloud_side_effect(_kind, _rows):
        assert session.commit_count == 1
        raise RuntimeError("simulated crash after Cloud accepted push")

    monkeypatch.setattr(drainer, "_push", fail_after_cloud_side_effect)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await drainer._drain_locked(session)
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_write_alarm_event_adds_event_and_outbox_in_one_commit(monkeypatch):
    from app.models import AlarmEvent, CloudSyncOutbox
    from app.services import alarm_event as ae_module

    session = _FakeSession()
    monkeypatch.setattr(ae_module, "AsyncSessionLocal", lambda: session)
    ae_module.settings.CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS = 10

    raised_at = dt.datetime(2026, 5, 20, 1, 2, 3, tzinfo=dt.timezone.utc)
    await ae_module.write_alarm_event(
        severity="critical",
        source_service="edge-monitor",
        alarm_key="cpu.high",
        tenant_id="t-1",
        line_id="L1",
        node_id="node-a",
        message="cpu over 95%",
        state="firing",
        raised_at=raised_at,
        meta={"value": 97},
    )

    assert session.committed is True
    assert session.rolled_back is False
    assert len(session.added) == 2
    assert isinstance(session.added[0], AlarmEvent)
    assert isinstance(session.added[1], CloudSyncOutbox)

    event = session.added[0]
    assert event.severity == "critical"
    assert event.source_service == "edge-monitor"
    assert event.alarm_key == "cpu.high"
    assert event.tenant_id == "t-1"
    assert event.line_id == "L1"
    assert event.node_id == "node-a"
    assert event.state == "firing"
    assert event.raised_at == raised_at
    assert event.first_seen_at == raised_at
    assert event.last_seen_at == raised_at
    assert event.meta == {"value": 97}

    outbox = session.added[1]
    assert outbox.kind == "alarm"
    assert outbox.max_attempts == 10
    assert outbox.payload["edge_alarm_id"] == 9
    assert outbox.payload["severity"] == "critical"
    assert outbox.payload["alarm_key"] == "cpu.high"
    assert outbox.payload["raised_at"] == raised_at.isoformat()
    assert outbox.payload["first_seen_at"] == raised_at.isoformat()
    assert outbox.payload["meta"] == {"value": 97}


@pytest.mark.asyncio
async def test_write_alarm_event_rolls_back_when_outbox_insert_fails(monkeypatch):
    from app.services import alarm_event as ae_module

    session = _FakeSession(fail_on_add=2)
    monkeypatch.setattr(ae_module, "AsyncSessionLocal", lambda: session)

    await ae_module.write_alarm_event(
        severity="warning",
        source_service="edge-monitor",
        alarm_key="disk.low",
    )

    assert session.committed is False
    assert session.rolled_back is True


@pytest.mark.asyncio
async def test_apply_alarm_transition_inserts_when_no_open_firing(monkeypatch):
    from app.models import AlarmEvent, CloudSyncOutbox
    from app.services import alarm_event as ae_module

    session = _FakeSession(rows=[])
    monkeypatch.setattr(ae_module, "AsyncSessionLocal", lambda: session)
    ae_module.settings.CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS = 10

    result = await ae_module.apply_alarm_transition(
        alarm_key="L1:edge-daq:health:down",
        source_service="edge-monitor",
        state="firing",
        severity="critical",
        tenant_id="t-1",
        line_id="L1",
        message="edge-daq down",
        meta={"detail": "container_exited"},
    )

    assert result["action"] == "inserted"
    assert result["alarm_id"] == 9
    assert session.committed is True
    assert len(session.added) == 2
    event = session.added[0]
    assert isinstance(event, AlarmEvent)
    assert event.alarm_key == "L1:edge-daq:health:down"
    assert event.state == "firing"
    assert event.severity == "critical"
    assert event.occurrence_count == 1
    outbox = session.added[1]
    assert isinstance(outbox, CloudSyncOutbox)
    assert outbox.kind == "alarm"
    assert outbox.payload["edge_alarm_id"] == 9
    assert outbox.payload["state"] == "firing"
    assert outbox.payload["severity"] == "critical"


@pytest.mark.asyncio
async def test_apply_alarm_transition_updates_existing_firing(monkeypatch):
    from app.models import AlarmEvent, CloudSyncOutbox
    from app.services import alarm_event as ae_module

    raised = dt.datetime(2026, 5, 20, 1, 0, 0, tzinfo=dt.timezone.utc)
    existing = AlarmEvent(
        tenant_id="t-1",
        line_id="L1",
        node_id=None,
        node_name=None,
        device_id=None,
        severity="warning",
        source_service="edge-monitor",
        alarm_key="L1:edge-daq:health:degraded",
        message="degraded",
        state="firing",
        raised_at=raised,
        first_seen_at=raised,
        last_seen_at=raised,
        occurrence_count=3,
        meta={"prev": True},
    )
    existing.id = 42
    existing.resolved_at = None

    session = _FakeSession(rows=[existing])
    monkeypatch.setattr(ae_module, "AsyncSessionLocal", lambda: session)

    now = dt.datetime(2026, 5, 20, 1, 5, 0, tzinfo=dt.timezone.utc)
    result = await ae_module.apply_alarm_transition(
        alarm_key="L1:edge-daq:health:degraded",
        source_service="edge-monitor",
        state="firing",
        severity="critical",  # 升级
        tenant_id="t-1",
        line_id="L1",
        message="now down",
        occurred_at=now,
        meta={"hit": 4},
    )

    assert result == {"action": "updated", "alarm_id": 42}
    assert session.committed is True
    assert existing.severity == "critical"  # warning → critical 升级
    assert existing.occurrence_count == 4
    assert existing.last_seen_at == now
    assert existing.message == "now down"
    assert existing.meta == {"hit": 4}
    assert existing.state == "firing"
    assert existing.resolved_at is None
    # outbox 单独 add，event 是 update 不应再次 add
    assert len(session.added) == 1
    assert isinstance(session.added[0], CloudSyncOutbox)
    assert session.added[0].payload["edge_alarm_id"] == 42
    assert session.added[0].payload["occurrence_count"] == 4


@pytest.mark.asyncio
async def test_apply_alarm_transition_resolved_closes_open_firing(monkeypatch):
    from app.models import AlarmEvent, CloudSyncOutbox
    from app.services import alarm_event as ae_module

    raised = dt.datetime(2026, 5, 20, 1, 0, 0, tzinfo=dt.timezone.utc)
    existing = AlarmEvent(
        tenant_id="t-1",
        line_id="L1",
        node_id=None,
        node_name=None,
        device_id=None,
        severity="critical",
        source_service="edge-monitor",
        alarm_key="L1:edge-daq:health:down",
        message="down",
        state="firing",
        raised_at=raised,
        first_seen_at=raised,
        last_seen_at=raised,
        occurrence_count=2,
        meta={},
    )
    existing.id = 77
    existing.resolved_at = None

    session = _FakeSession(rows=[existing])
    monkeypatch.setattr(ae_module, "AsyncSessionLocal", lambda: session)

    now = dt.datetime(2026, 5, 20, 1, 10, 0, tzinfo=dt.timezone.utc)
    result = await ae_module.apply_alarm_transition(
        alarm_key="L1:edge-daq:health:down",
        source_service="edge-monitor",
        state="resolved",
        tenant_id="t-1",
        line_id="L1",
        occurred_at=now,
    )

    assert result == {"action": "updated", "alarm_id": 77}
    assert existing.state == "resolved"
    assert existing.resolved_at == now
    assert session.committed is True
    assert len(session.added) == 1
    outbox = session.added[0]
    assert isinstance(outbox, CloudSyncOutbox)
    assert outbox.payload["state"] == "resolved"
    assert outbox.payload["resolved_at"] == now.isoformat()


@pytest.mark.asyncio
async def test_apply_alarm_transition_resolved_without_open_is_noop(monkeypatch):
    from app.services import alarm_event as ae_module

    session = _FakeSession(rows=[])
    monkeypatch.setattr(ae_module, "AsyncSessionLocal", lambda: session)

    result = await ae_module.apply_alarm_transition(
        alarm_key="L1:edge-daq:health:down",
        source_service="edge-monitor",
        state="resolved",
        tenant_id="t-1",
        line_id="L1",
    )

    assert result == {"action": "noop", "alarm_id": None}
    assert session.committed is False
    assert session.rolled_back is True
    assert session.added == []


@pytest.mark.asyncio
async def test_apply_alarm_transition_rejects_invalid_state(monkeypatch):
    from app.services import alarm_event as ae_module

    with pytest.raises(ValueError):
        await ae_module.apply_alarm_transition(
            alarm_key="x",
            source_service="edge-monitor",
            state="bogus",
        )


@pytest.mark.asyncio
async def test_ack_alarm_event_marks_existing_and_writes_outbox(monkeypatch):
    from app.models import AlarmEvent, CloudSyncOutbox
    from app.services import alarm_event as ae_module

    existing = AlarmEvent(
        tenant_id="t-1",
        line_id="L1",
        severity="critical",
        source_service="edge-monitor",
        alarm_key="L1:edge-daq:health",
        state="firing",
        raised_at=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        first_seen_at=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        last_seen_at=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        occurrence_count=2,
        meta={"old": True},
    )
    existing.id = 55
    existing.ack_at = None
    existing.ack_by = None
    existing.resolved_at = None

    session = _FakeSession(rows=[existing])
    monkeypatch.setattr(ae_module, "AsyncSessionLocal", lambda: session)

    result = await ae_module.ack_alarm_event(alarm_id=55, operator="zhangsan", note="已通知现场")

    assert result == {"action": "acknowledged", "alarm_id": 55}
    assert existing.ack_at is not None
    assert existing.ack_by == "zhangsan"
    assert existing.meta == {"old": True, "note": "已通知现场"}
    assert session.committed is True
    assert len(session.added) == 1
    outbox = session.added[0]
    assert isinstance(outbox, CloudSyncOutbox)
    assert outbox.kind == "alarm"
    assert outbox.payload["edge_alarm_id"] == 55
    assert outbox.payload["ack_by"] == "zhangsan"
    assert outbox.payload["meta"]["note"] == "已通知现场"


@pytest.mark.asyncio
async def test_ack_alarm_event_default_operator_unknown(monkeypatch):
    from app.models import AlarmEvent
    from app.services import alarm_event as ae_module

    existing = AlarmEvent(
        severity="warning",
        source_service="edge-monitor",
        alarm_key="L1:x:health",
        state="firing",
        raised_at=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        first_seen_at=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        last_seen_at=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        occurrence_count=1,
        meta={},
    )
    existing.id = 1
    existing.ack_at = None
    existing.ack_by = None
    existing.resolved_at = None

    session = _FakeSession(rows=[existing])
    monkeypatch.setattr(ae_module, "AsyncSessionLocal", lambda: session)

    # operator 空字符串应该回退到 "unknown"
    result = await ae_module.ack_alarm_event(alarm_id=1, operator="", note=None)

    assert result["action"] == "acknowledged"
    assert existing.ack_by == "unknown"
    # 没传 note → meta 不应被改
    assert existing.meta == {}


@pytest.mark.asyncio
async def test_ack_alarm_event_truncates_long_note(monkeypatch):
    from app.models import AlarmEvent
    from app.services import alarm_event as ae_module

    existing = AlarmEvent(
        severity="warning",
        source_service="edge-monitor",
        alarm_key="L1:x:health",
        state="firing",
        raised_at=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        first_seen_at=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        last_seen_at=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        occurrence_count=1,
        meta={},
    )
    existing.id = 2
    existing.ack_at = None
    existing.ack_by = None
    existing.resolved_at = None

    session = _FakeSession(rows=[existing])
    monkeypatch.setattr(ae_module, "AsyncSessionLocal", lambda: session)

    long_note = "x" * 1000
    await ae_module.ack_alarm_event(alarm_id=2, operator="op", note=long_note)

    assert len(existing.meta["note"]) == 256


@pytest.mark.asyncio
async def test_ack_alarm_event_not_found(monkeypatch):
    from app.services import alarm_event as ae_module

    session = _FakeSession(rows=[])
    monkeypatch.setattr(ae_module, "AsyncSessionLocal", lambda: session)

    result = await ae_module.ack_alarm_event(alarm_id=999, operator="op", note=None)
    assert result == {"action": "not_found", "alarm_id": 999}
    assert session.committed is False
    assert session.rolled_back is True


@pytest.mark.asyncio
async def test_ack_alarm_event_is_idempotent(monkeypatch):
    """重复 ACK 应该成功（覆盖 ack_at），每次也写一条 outbox 让云端同步。"""
    from app.models import AlarmEvent
    from app.services import alarm_event as ae_module

    first_ack = dt.datetime(2026, 5, 20, 1, 0, 0, tzinfo=dt.timezone.utc)
    existing = AlarmEvent(
        severity="warning",
        source_service="edge-monitor",
        alarm_key="L1:x:health",
        state="firing",
        raised_at=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        first_seen_at=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        last_seen_at=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        occurrence_count=1,
        meta={"note": "first"},
    )
    existing.id = 3
    existing.ack_at = first_ack
    existing.ack_by = "alice"
    existing.resolved_at = None

    session = _FakeSession(rows=[existing])
    monkeypatch.setattr(ae_module, "AsyncSessionLocal", lambda: session)

    result = await ae_module.ack_alarm_event(alarm_id=3, operator="bob", note="second")
    assert result["action"] == "acknowledged"
    assert existing.ack_by == "bob"
    assert existing.ack_at is not None and existing.ack_at > first_ack
    assert existing.meta["note"] == "second"


@pytest.mark.asyncio
async def test_outbox_drain_pushes_alarm_to_alarm_path(monkeypatch):
    from app.models import CloudSyncOutbox
    from app.services import outbox_drain as drain_module
    from app.services.outbox_drain import CloudSyncOutboxDrainer

    monkeypatch.setattr(drain_module.settings, "CLOUD_OPS_BASE_URL", "https://cloud.example")
    monkeypatch.setattr(drain_module, "load_runtime_identity", lambda: {"runtime_credential": "tk-alarm"})
    monkeypatch.setattr(drain_module.settings, "CLOUD_SYNC_OUTBOX_ALARM_PATH", "/api/v1/alarms/from-edge")

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

    row = CloudSyncOutbox(kind="alarm", payload={"edge_alarm_id": 1, "alarm_key": "cpu.high"})
    drainer = CloudSyncOutboxDrainer()

    ok, err = await drainer._push("alarm", [row])

    assert ok is True
    assert err == ""
    assert captured["url"] == "https://cloud.example/api/v1/alarms/from-edge"
    assert captured["json"] == {"items": [row.payload]}
    assert captured["headers"]["X-Device-Credential"] == "tk-alarm"
