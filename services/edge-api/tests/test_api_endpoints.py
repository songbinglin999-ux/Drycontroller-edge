"""
FastAPI 路由级别测试（依赖注入 stub）
"""

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]
for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

from app.api.v1.ai import router as ai_router, get_ai_softsensor_service
from app.api.v1.system import _bootstrap_summary, router as system_router, get_system_status_service
from app.api.v1.control import router as control_router, get_control_recommendation_service
from app.api.v1.alarms import router as alarm_router
from app.core.config import settings
from app.core.database import get_db
from app.services.control_command import get_control_command_service

settings.CONTROL_API_KEYS = ["test-control"]
settings.ADMIN_API_KEYS = ["test-admin"]


class DummyAISvc:
    async def get_lab_samples(self, **kwargs):
        if kwargs.get("start") == "bad":
            raise ValueError("Invalid start range format.")
        return {
            "success": True,
            "count": 1,
            "data": [
                {"time": "2025-01-01T00:00:00Z", "line_id": "L1", "moisture_lab": 12.8}
            ],
        }


class DummySystemSvc:
    async def get_metric_trend(self, **kwargs):
        return {"success": True, "count": 1, "data": [{"time": "2025-01-01", "value": 40}]}


class DummyControlSvc:
    async def get_recommendations(self, **kwargs):
        return {
            "success": True,
            "data": {
                "line": "L1",
                "recommendations": {"CTRL_TEST_SPEED": 35.5},
            },
        }


class _DummyScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _DummyExecResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _DummyScalars(self._rows)


class _DummyDB:
    """最小 FastAPI Depends-friendly stub for AsyncSession."""

    rows: list = []

    async def execute(self, _stmt):
        return _DummyExecResult(_DummyDB.rows)


async def _override_get_db():
    yield _DummyDB()


app = FastAPI()
app.include_router(ai_router, prefix="/api/v1/ai")
app.include_router(system_router, prefix="/api/v1/system")
app.include_router(control_router, prefix="/api/v1/control")
app.include_router(alarm_router, prefix="/api/v1/alarms")
app.dependency_overrides[get_ai_softsensor_service] = lambda: DummyAISvc()
app.dependency_overrides[get_system_status_service] = lambda: DummySystemSvc()
app.dependency_overrides[get_control_recommendation_service] = lambda: DummyControlSvc()
app.dependency_overrides[get_db] = _override_get_db
app.dependency_overrides[get_control_command_service] = lambda: DummyControlCommandSvc()
client = TestClient(app)


def test_ai_lab_samples_endpoint():
    resp = client.get("/api/v1/ai/lab/samples?start=-1d&limit=1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["data"][0]["line_id"] == "L1"


def test_ai_lab_samples_validation_error_returns_400():
    resp = client.get("/api/v1/ai/lab/samples?start=bad")
    assert resp.status_code == 400


def test_system_metric_trend_endpoint():
    resp = client.get("/api/v1/system/metrics/trend?field=cpu_percent")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


def test_bootstrap_summary_accepts_legacy_sentinel(tmp_path):
    sentinel = tmp_path / ".bootstrapped"
    sentinel.write_text("ok", encoding="utf-8")

    item = _bootstrap_summary(sentinel_path=sentinel, activation_state_path=tmp_path / "missing.json")

    assert item["status"] == "ok"
    assert item["message"] == "已完成"


def test_bootstrap_summary_accepts_formal_activation_state(tmp_path):
    activation_state = tmp_path / "activation-state.json"
    activation_state.write_text('{"state":"formal_license_active"}', encoding="utf-8")

    item = _bootstrap_summary(sentinel_path=tmp_path / "missing", activation_state_path=activation_state)

    assert item["status"] == "ok"
    assert item["message"] == "正式授权已激活"


def test_bootstrap_summary_degraded_without_completion(tmp_path):
    item = _bootstrap_summary(sentinel_path=tmp_path / "missing", activation_state_path=tmp_path / "missing.json")

    assert item["status"] == "degraded"
    assert item["message"] == "未检测到完成标记"


def test_control_recommend_endpoint():
    resp = client.get(
        "/api/v1/control/recommend",
        headers={settings.API_KEY_HEADER: "test-control"},
    )
    assert resp.status_code == 200
    assert "recommendations" in resp.json()["data"]


class DummyControlCommandSvc:
    async def send_set(self, **kwargs):
        # 模拟 ACK 成功闭环
        return {
            "success": True,
            "status": "acked",
            "data": {"id": "ctl_x", "tag": kwargs.get("tag"), "ack_status": "success", "value": kwargs.get("value")},
            "timestamp": 1,
        }

    async def send_batch(self, **kwargs):
        return {"success": True, "count": 1, "data": [{"success": True}], "timestamp": 1}

    async def emergency_stop(self, **kwargs):
        return {"success": True, "count": 1, "data": [{"success": True}], "timestamp": 1}


def test_control_set_endpoint():
    resp = client.post(
        "/api/v1/control/set",
        json={"tag": "CTRL_TEST_SPEED", "value": 10, "wait_ack": True, "timeout_sec": 1.0},
        headers={settings.API_KEY_HEADER: "test-control"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["status"] == "acked"


def test_alarm_active_endpoint_reads_alarm_events_table():
    import datetime as _dt
    from app.models import AlarmEvent

    row = AlarmEvent(
        tenant_id="t-1",
        line_id="L1",
        severity="critical",
        source_service="edge-monitor",
        alarm_key="L1:edge-daq:health",
        state="firing",
        raised_at=_dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc),
        first_seen_at=_dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc),
        last_seen_at=_dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc),
        occurrence_count=1,
        meta={},
        message="boom",
    )
    row.id = 5
    row.ack_at = None
    row.ack_by = None
    row.resolved_at = None
    row.silenced_until = None
    row.node_id = None
    row.node_name = None
    row.device_id = None
    _DummyDB.rows = [row]
    try:
        resp = client.get("/api/v1/alarms/active?period=10m")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["count"] == 1
        assert body["data"][0]["id"] == 5
        assert body["data"][0]["severity"] == "critical"
        assert body["data"][0]["alarm_key"] == "L1:edge-daq:health"
        assert body["data"][0]["state"] == "firing"
    finally:
        _DummyDB.rows = []


def test_alarm_active_invalid_period_returns_400():
    resp = client.get("/api/v1/alarms/active?period=bogus")
    assert resp.status_code == 400


def test_alarm_history_reads_alarm_events_table():
    import datetime as _dt
    from app.models import AlarmEvent

    row = AlarmEvent(
        tenant_id="t-1",
        line_id="L1",
        severity="warning",
        source_service="edge-monitor",
        alarm_key="L1:edge-api:health",
        state="resolved",
        raised_at=_dt.datetime(2026, 5, 19, tzinfo=_dt.timezone.utc),
        first_seen_at=_dt.datetime(2026, 5, 19, tzinfo=_dt.timezone.utc),
        last_seen_at=_dt.datetime(2026, 5, 19, tzinfo=_dt.timezone.utc),
        occurrence_count=2,
        meta={},
    )
    row.id = 6
    row.ack_at = None
    row.ack_by = None
    row.resolved_at = _dt.datetime(2026, 5, 19, 1, 0, tzinfo=_dt.timezone.utc)
    row.silenced_until = None
    row.node_id = None
    row.node_name = None
    row.device_id = None
    row.message = None
    _DummyDB.rows = [row]
    try:
        resp = client.get("/api/v1/alarms/history?period=24h")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["data"][0]["state"] == "resolved"
        assert body["data"][0]["resolved_at"] is not None
    finally:
        _DummyDB.rows = []


def test_alarm_internal_ingest_requires_admin_key():
    resp = client.post(
        "/api/v1/alarms/internal/ingest",
        json={
            "alarm_key": "L1:edge-daq:health:down",
            "source_service": "edge-monitor",
            "state": "firing",
            "severity": "critical",
        },
    )
    assert resp.status_code == 401

    resp = client.post(
        "/api/v1/alarms/internal/ingest",
        json={
            "alarm_key": "L1:edge-daq:health:down",
            "source_service": "edge-monitor",
            "state": "firing",
            "severity": "critical",
        },
        headers={settings.API_KEY_HEADER: "wrong"},
    )
    assert resp.status_code == 403

    resp = client.post(
        "/api/v1/alarms/internal/ingest",
        json={
            "alarm_key": "L1:edge-daq:health:down",
            "source_service": "edge-monitor",
            "state": "firing",
            "severity": "critical",
        },
        headers={settings.API_KEY_HEADER: "test-control"},  # control 级，不够
    )
    assert resp.status_code == 403


def test_alarm_internal_ingest_invokes_apply_transition(monkeypatch):
    from app.api.v1 import alarms as alarms_module

    captured: dict = {}

    async def fake_apply(**kwargs):
        captured.update(kwargs)
        return {"action": "inserted", "alarm_id": 123}

    monkeypatch.setattr(alarms_module, "apply_alarm_transition", fake_apply)

    resp = client.post(
        "/api/v1/alarms/internal/ingest",
        json={
            "alarm_key": "L1:edge-daq:health:down",
            "source_service": "edge-monitor",
            "state": "firing",
            "severity": "critical",
            "tenant_id": "t-1",
            "line_id": "L1",
            "node_name": "edge-01",
            "message": "container exited",
            "meta": {"reason": "exit_code=137"},
        },
        headers={settings.API_KEY_HEADER: "test-admin"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["action"] == "inserted"
    assert body["alarm_id"] == 123

    assert captured["alarm_key"] == "L1:edge-daq:health:down"
    assert captured["state"] == "firing"
    assert captured["severity"] == "critical"
    assert captured["tenant_id"] == "t-1"
    assert captured["line_id"] == "L1"
    assert captured["meta"] == {"reason": "exit_code=137"}


def test_alarm_internal_ingest_rejects_invalid_state(monkeypatch):
    from app.api.v1 import alarms as alarms_module

    async def fake_apply(**_kwargs):
        raise ValueError("invalid alarm state: 'bogus'")

    monkeypatch.setattr(alarms_module, "apply_alarm_transition", fake_apply)

    resp = client.post(
        "/api/v1/alarms/internal/ingest",
        json={
            "alarm_key": "x",
            "source_service": "edge-monitor",
            "state": "bogus",
        },
        headers={settings.API_KEY_HEADER: "test-admin"},
    )
    assert resp.status_code == 400


def test_alarm_acknowledge_requires_admin_key():
    resp = client.post("/api/v1/alarms/42/acknowledge", json={"operator": "op"})
    assert resp.status_code == 401

    resp = client.post(
        "/api/v1/alarms/42/acknowledge",
        json={"operator": "op"},
        headers={settings.API_KEY_HEADER: "test-control"},  # control 级，不够
    )
    assert resp.status_code == 403


def test_alarm_acknowledge_success(monkeypatch):
    from app.api.v1 import alarms as alarms_module

    captured: dict = {}

    async def fake_ack(*, alarm_id, operator, note):
        captured.update({"alarm_id": alarm_id, "operator": operator, "note": note})
        return {"action": "acknowledged", "alarm_id": alarm_id}

    monkeypatch.setattr(alarms_module, "ack_alarm_event", fake_ack)

    resp = client.post(
        "/api/v1/alarms/42/acknowledge",
        json={"operator": "zhangsan", "note": "已通知现场"},
        headers={settings.API_KEY_HEADER: "test-admin"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["action"] == "acknowledged"
    assert body["alarm_id"] == 42
    assert captured == {"alarm_id": 42, "operator": "zhangsan", "note": "已通知现场"}


def test_alarm_acknowledge_default_operator(monkeypatch):
    from app.api.v1 import alarms as alarms_module

    captured: dict = {}

    async def fake_ack(*, alarm_id, operator, note):
        captured.update({"alarm_id": alarm_id, "operator": operator, "note": note})
        return {"action": "acknowledged", "alarm_id": alarm_id}

    monkeypatch.setattr(alarms_module, "ack_alarm_event", fake_ack)

    # 不传 body
    resp = client.post(
        "/api/v1/alarms/42/acknowledge",
        headers={settings.API_KEY_HEADER: "test-admin"},
    )
    assert resp.status_code == 200
    assert captured["operator"] == "unknown"
    assert captured["note"] is None


def test_alarm_acknowledge_not_found(monkeypatch):
    from app.api.v1 import alarms as alarms_module

    async def fake_ack(*, alarm_id, operator, note):
        return {"action": "not_found", "alarm_id": alarm_id}

    monkeypatch.setattr(alarms_module, "ack_alarm_event", fake_ack)

    resp = client.post(
        "/api/v1/alarms/999/acknowledge",
        json={"operator": "op"},
        headers={settings.API_KEY_HEADER: "test-admin"},
    )
    assert resp.status_code == 404
