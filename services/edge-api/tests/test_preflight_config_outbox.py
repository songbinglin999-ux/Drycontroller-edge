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


class _FakeSession:
    def __init__(self):
        self.added = []
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        row = self.added[0]
        row.id = 7
        now = dt.datetime(2026, 5, 22, 1, 2, 3, tzinfo=dt.timezone.utc)
        if getattr(row, "run_at", None) is None:
            row.run_at = now
        if getattr(row, "applied_at", None) is None:
            row.applied_at = now

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_record_preflight_run_adds_local_row_and_outbox(monkeypatch):
    from app.models import CloudSyncOutbox, PreflightRun
    from app.services import preflight_history as history_module

    session = _FakeSession()
    monkeypatch.setattr(history_module, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(history_module.settings, "CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS", 10)
    monkeypatch.setattr(
        history_module,
        "load_edge_identity",
        lambda: {"edge_device_id": "EDGE-7", "edge_node_name": "edge-7"},
    )

    await history_module.record_preflight_run(
        {"success": True, "status": "degraded", "summary": {"pass": 9, "degraded": 2, "fail": 0}}
    )

    assert session.committed is True
    assert session.rolled_back is False
    assert isinstance(session.added[0], PreflightRun)
    assert isinstance(session.added[1], CloudSyncOutbox)

    row = session.added[0]
    assert row.status == "degraded"
    assert row.ok is False
    assert row.json_output["summary"]["degraded"] == 2

    outbox = session.added[1]
    assert outbox.kind == "preflight_run"
    assert outbox.max_attempts == 10
    assert outbox.payload["edge_preflight_id"] == 7
    assert outbox.payload["edge_device_id"] == "EDGE-7"
    assert outbox.payload["edge_node_name"] == "edge-7"
    assert outbox.payload["status"] == "degraded"


@pytest.mark.asyncio
async def test_record_config_version_adds_local_row_and_outbox(monkeypatch):
    from app.models import CloudSyncOutbox, ConfigVersion
    from app.services import config_version as version_module

    session = _FakeSession()
    monkeypatch.setattr(version_module, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(version_module.settings, "CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS", 10)
    monkeypatch.setattr(
        version_module,
        "load_edge_identity",
        lambda: {"edge_device_id": "EDGE-7", "edge_node_name": "edge-7"},
    )

    await version_module.record_config_version(
        scope="plc",
        payload={"host": "192.168.1.20", "port": 502},
        source="cloud",
        applied_by="admin:key:abc",
        restart_required=True,
    )

    assert session.committed is True
    assert isinstance(session.added[0], ConfigVersion)
    assert isinstance(session.added[1], CloudSyncOutbox)

    row = session.added[0]
    assert row.scope == "plc"
    assert row.version.startswith("sha256:")
    assert len(row.checksum) == 64
    assert row.restart_required is True

    outbox = session.added[1]
    assert outbox.kind == "config_version"
    assert outbox.payload["edge_config_id"] == 7
    assert outbox.payload["edge_device_id"] == "EDGE-7"
    assert outbox.payload["edge_node_name"] == "edge-7"
    assert outbox.payload["payload"]["host"] == "192.168.1.20"
    assert outbox.payload["applied_by"] == "admin:key:abc"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "path_setting", "path"),
    [
        ("preflight_run", "CLOUD_SYNC_OUTBOX_PREFLIGHT_PATH", "/api/v1/preflight/from-edge"),
        ("config_version", "CLOUD_SYNC_OUTBOX_CONFIG_PATH", "/api/v1/config-versions/from-edge"),
    ],
)
async def test_outbox_drain_pushes_new_history_kinds(monkeypatch, kind, path_setting, path):
    from app.models import CloudSyncOutbox
    from app.services import outbox_drain as drain_module
    from app.services.outbox_drain import CloudSyncOutboxDrainer

    monkeypatch.setattr(drain_module.settings, "CLOUD_OPS_BASE_URL", "https://cloud.example")
    monkeypatch.setenv("CLOUD_OPS_ENROLL_TOKEN", "tk-history")
    monkeypatch.setattr(drain_module.settings, path_setting, path)
    monkeypatch.setattr(drain_module.settings, "CLOUD_SYNC_OUTBOX_ENROLL_TOKEN_HEADER", "X-Enroll-Token")

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

    row = CloudSyncOutbox(kind=kind, payload={"edge_id": 1})
    ok, err = await CloudSyncOutboxDrainer()._push(kind, [row])

    assert ok is True
    assert err == ""
    assert captured["url"] == f"https://cloud.example{path}"
    assert captured["json"] == {"items": [row.payload]}
    assert captured["headers"]["X-Enroll-Token"] == "tk-history"
