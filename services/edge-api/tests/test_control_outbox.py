from __future__ import annotations

import datetime as dt
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
        self.added[0].id = 7

    async def execute(self, _stmt):
        return _FakeExecuteResult(self.rows)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_write_control_history_adds_history_and_outbox_in_one_commit(monkeypatch):
    from app.models import CloudSyncOutbox, ControlCommandHistory
    from app.services import control_history as ch_module

    session = _FakeSession()
    monkeypatch.setattr(ch_module, "AsyncSessionLocal", lambda: session)
    ch_module.settings.CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS = 10

    ack_at = dt.datetime(2026, 5, 18, 1, 2, 3, tzinfo=dt.timezone.utc)
    await ch_module.write_control_history(
        command_type="set",
        tag="DRYER1.FAN.SPEED",
        value=12,
        line_id="L1",
        idempotency_key="ctl_abc",
        request_payload={"line": "L1", "tag": "DRYER1.FAN.SPEED", "value": 12},
        ack_status="ok",
        ack_at=ack_at,
        ack_detail={"ack_ts": 1},
    )

    assert session.committed is True
    assert session.rolled_back is False
    assert len(session.added) == 2
    assert isinstance(session.added[0], ControlCommandHistory)
    assert isinstance(session.added[1], CloudSyncOutbox)

    history = session.added[0]
    assert history.command_type == "set"
    assert history.tag == "DRYER1.FAN.SPEED"
    assert history.value == "12"  # Text 列存字符串
    assert history.line_id == "L1"
    assert history.ack_status == "ok"

    outbox = session.added[1]
    assert outbox.kind == "control_command"
    assert outbox.max_attempts == 10
    assert outbox.payload["edge_control_id"] == 7
    assert outbox.payload["command_type"] == "set"
    assert outbox.payload["value"] == "12"
    assert outbox.payload["ack_status"] == "ok"
    assert outbox.payload["ack_at"] == ack_at.isoformat()
    assert outbox.payload["idempotency_key"] == "ctl_abc"


@pytest.mark.asyncio
async def test_write_control_history_rolls_back_when_outbox_insert_fails(monkeypatch):
    from app.services import control_history as ch_module

    session = _FakeSession(fail_on_add=2)
    monkeypatch.setattr(ch_module, "AsyncSessionLocal", lambda: session)

    await ch_module.write_control_history(command_type="set", tag="X", value=1)

    assert session.committed is False
    assert session.rolled_back is True


@pytest.mark.asyncio
async def test_outbox_drain_pushes_control_command_to_control_path(monkeypatch):
    from app.models import CloudSyncOutbox
    from app.services import outbox_drain as drain_module
    from app.services.outbox_drain import CloudSyncOutboxDrainer

    drain_module.settings.CLOUD_OPS_BASE_URL = "https://cloud.example"
    monkeypatch.setenv("CLOUD_OPS_ENROLL_TOKEN", "tk-control")
    drain_module.settings.CLOUD_SYNC_OUTBOX_CONTROL_PATH = "/api/v1/control/from-edge"
    drain_module.settings.CLOUD_SYNC_OUTBOX_ENROLL_TOKEN_HEADER = "X-Enroll-Token"

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

    row = CloudSyncOutbox(kind="control_command", payload={"edge_control_id": 1, "command_type": "set"})
    drainer = CloudSyncOutboxDrainer()

    ok, err = await drainer._push("control_command", [row])

    assert ok is True
    assert err == ""
    assert captured["url"] == "https://cloud.example/api/v1/control/from-edge"
    assert captured["json"] == {"items": [row.payload]}
    assert captured["headers"]["X-Enroll-Token"] == "tk-control"


@pytest.mark.asyncio
async def test_outbox_drain_unknown_kind_returns_unsupported(monkeypatch):
    from app.models import CloudSyncOutbox
    from app.services.outbox_drain import CloudSyncOutboxDrainer

    row = CloudSyncOutbox(kind="alarm_event", payload={})
    drainer = CloudSyncOutboxDrainer()

    ok, err = await drainer._push("alarm_event", [row])

    assert ok is False
    assert err.startswith("unsupported_outbox_kind:")
