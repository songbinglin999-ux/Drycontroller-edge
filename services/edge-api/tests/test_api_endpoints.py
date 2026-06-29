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
from app.api.v1.control import (
    get_control_orchestrator,
    get_control_state_service,
    router as control_router,
)
from app.api.v1.alarms import router as alarm_router
from app.core.config import settings
from app.core.database import get_db

settings.CONTROL_API_KEYS = ["test-control"]
settings.ADMIN_API_KEYS = ["test-admin"]
settings.READONLY_WS_ALLOWLIST = ["testclient"]


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
                "schema_version": "drycontroller.control_recommendation.v1",
                "line_id": "L1",
                "freshness": {"status": "fresh", "missing_tags": [], "stale_tags": []},
                "control_state": {
                    "CONTROL_MODE": "AUTO",
                    "AUTHORITY_OWNER": "EDGE",
                    "HMI_DIRECT_ENABLE": False,
                    "maintenance_lock": False,
                    "emergency_stop": False,
                },
                "recommendations": [],
                "allowed_targets": ["VFM3_FREQ_SP"],
                "requires_confirmation": False,
                "production_requires_confirmation": False,
                "operation_modes": ["MANUAL", "AUTO"],
                "non_selectable_modes": ["AUTO_PAUSED"],
                "auto_pause_state": "AUTO_PAUSED",
                "auto_pause_is_operator_mode": False,
                "recommendation_confirmation_scope": "commissioning_only",
            },
        }


class DummyControlOrchestrator:
    async def get_recommendations(self, **kwargs):
        return await DummyControlSvc().get_recommendations(**kwargs)

    async def confirm_recommendation(self, **kwargs):
        return {
            "success": True,
            "status": "confirmed",
            "recommendation": {"id": kwargs["recommendation_id"]},
            "command_results": [{"success": True, "status": "acked"}],
        }

    async def confirm_manual_action(self, **kwargs):
        return {
            "success": True,
            "status": "confirmed",
            "recommendation": {"id": kwargs["request_id"], "line_id": kwargs["line"]},
            "command_results": [{"success": True, "status": "acked"}],
        }

    async def dismiss_recommendation(self, **kwargs):
        return {"success": True, "status": "dismissed", "id": kwargs["recommendation_id"]}


class DummyControlState:
    def __init__(self):
        self.mode = "MANUAL"
        self.last_manual_result = None

    async def get_status(self, **kwargs):
        return {
            "mode": self.mode,
            "operation_modes": ["MANUAL", "AUTO"],
            "non_selectable_modes": ["AUTO_PAUSED"],
            "auto_pause_state": "AUTO_PAUSED",
            "auto_paused_display": "自动暂停（需重新授权）",
            "auto_pause_is_operator_mode": False,
            "auto_dispatch_status": "manual_stopped" if self.mode == "MANUAL" else "ready",
            "requires_confirmation": False,
            "production_requires_confirmation": False,
            "requires_reauthorization": False,
            "pause_reason": None,
            "last_transition": {"from": None, "to": self.mode, "reason": "test"},
            "authority_snapshot": None,
            "last_command_result": self.last_manual_result,
            "controllable_tags": ["VFM1_FREQ_SP"],
            "commissioning_requires_confirmation": True,
        }

    async def authorize_auto(self, **kwargs):
        self.mode = "AUTO"
        return {"success": True, "status": "AUTO", "data": await self.get_status()}

    async def switch_manual(self, **kwargs):
        self.mode = "MANUAL"
        return {"success": True, "status": "MANUAL", "data": await self.get_status()}

    async def dispatch_auto(self, **kwargs):
        return {"success": True, "status": "dispatched", "data": await self.get_status()}

    async def note_manual_intervention(self, **kwargs):
        self.mode = "MANUAL"
        return {"previous_mode": "AUTO", "current_mode": "MANUAL"}

    async def record_command_result(self, **kwargs):
        self.last_manual_result = {"scope": kwargs["command_scope"], "result": kwargs["result"]}

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
app.dependency_overrides[get_control_orchestrator] = lambda: DummyControlOrchestrator()
app.dependency_overrides[get_control_state_service] = lambda: DummyControlState()
app.dependency_overrides[get_db] = _override_get_db
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


def test_v1_readonly_routes_reject_non_allowlisted_source():
    old_allowlist = list(settings.READONLY_WS_ALLOWLIST)
    try:
        settings.READONLY_WS_ALLOWLIST = ["192.168.0.0/16"]
        for path in (
            "/api/v1/ai/lab/samples?start=-1d&limit=1",
            "/api/v1/system/metrics/trend?field=cpu_percent",
            "/api/v1/alarms/active?period=10m",
        ):
            resp = client.get(path)
            assert resp.status_code == 403
            assert resp.json()["detail"] == "Read-only data access is not allowed from this source"
    finally:
        settings.READONLY_WS_ALLOWLIST = old_allowlist


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


def test_legacy_control_recommend_endpoint_removed():
    resp = client.get(
        "/api/v1/control/recommend",
        headers={settings.API_KEY_HEADER: "test-control"},
    )
    assert resp.status_code == 404


def test_control_recommendations_endpoint():
    resp = client.get(
        "/api/v1/control/recommendations",
        headers={settings.API_KEY_HEADER: "test-control"},
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["schema_version"] == "drycontroller.control_recommendation.v1"
    assert body["allowed_targets"] == ["VFM3_FREQ_SP"]


def test_control_status_uses_mode_authorization_model():
    resp = client.get(
        "/api/v1/control/status",
        headers={settings.API_KEY_HEADER: "test-control"},
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["operation_modes"] == ["MANUAL", "AUTO"]
    assert body["non_selectable_modes"] == ["AUTO_PAUSED"]
    assert body["requires_confirmation"] is False
    assert body["production_requires_confirmation"] is False
    assert body["auto_pause_state"] == "AUTO_PAUSED"
    assert body["auto_pause_is_operator_mode"] is False
    assert body["auto_dispatch_status"] == "manual_stopped"
    assert body["commissioning_requires_confirmation"] is True


def test_control_mode_auto_authorize_endpoint():
    resp = client.post(
        "/api/v1/control/mode",
        json={"line": "L1", "mode": "AUTO", "operator_id": "operator-a", "reason": "shift_start"},
        headers={settings.API_KEY_HEADER: "test-control"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "AUTO"


def test_control_mode_rejects_auto_paused_as_operator_mode():
    resp = client.post(
        "/api/v1/control/mode",
        json={"line": "L1", "mode": "AUTO_PAUSED"},
        headers={settings.API_KEY_HEADER: "test-control"},
    )
    assert resp.status_code == 400


def test_control_auto_dispatch_endpoint():
    resp = client.post(
        "/api/v1/control/auto/dispatch",
        json={"line": "L1", "timeout_sec": 1.0},
        headers={settings.API_KEY_HEADER: "test-control"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "dispatched"


def test_control_recommendation_confirm_endpoint():
    resp = client.post(
        "/api/v1/control/recommendations/rec_l1_x/confirm",
        json={"line": "L1", "timeout_sec": 1.0},
        headers={settings.API_KEY_HEADER: "test-control"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"


def test_manual_control_confirm_endpoint():
    resp = client.post(
        "/api/v1/control/recommendations/manual-confirm",
        json={
            "line": "L1",
            "target_softpoint": "VFM1_FREQ_SP",
            "value": 20.0,
            "request_id": "manual-1",
            "operator_id": "operator-a",
            "reason": "hmi_panel_submit",
            "timeout_sec": 1.0,
        },
        headers={settings.API_KEY_HEADER: "test-control"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"


def test_control_recommendation_dismiss_endpoint():
    resp = client.post(
        "/api/v1/control/recommendations/rec_l1_x/dismiss",
        headers={settings.API_KEY_HEADER: "test-control"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"


def test_legacy_control_write_endpoints_removed():
    for path in (
        "/api/v1/control/set",
        "/api/v1/control/batch",
        "/api/v1/control/emergency-stop",
    ):
        resp = client.post(
            path,
            json={},
            headers={settings.API_KEY_HEADER: "test-control"},
        )
        assert resp.status_code == 404


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
