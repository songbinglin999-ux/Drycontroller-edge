import os
import sys
from pathlib import Path
import pytest
from fastapi import HTTPException
import httpx
from httpx import AsyncClient
from typing import cast

PATH = Path(__file__).resolve()
ROOT = PATH.parents[3]
SERVICE_ROOT = PATH.parents[1]
LOCAL_LOG_ROOT = ROOT / "logs"
LOCAL_LOG_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOG_ROOT", str(LOCAL_LOG_ROOT))
for path in {str(ROOT), str(SERVICE_ROOT)}:
    if path not in sys.path:
        sys.path.append(path)


from monitor.main import MonitorService  # noqa: E402


def _new_service():
    svc = MonitorService()
    svc.line_id = "L-test"
    return svc


def _enable_control_probe(service):
    service.control_status_enabled = True
    service.control_status_target = {
        "enabled": True,
        "url": "http://edge-api:8000/api/v1/control/status",
        "api_key_env": "ADMIN_API_KEYS",
        "api_key_header": "X-API-Key",
    }
    service.control_status = {
        "enabled": True,
        "status": "unknown",
        "self_heal": {"enabled": False, "reason": "control_visibility_probe_only"},
    }


class _ControlStatusResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


def test_build_status_payload_schema_and_services_sorted():
    service = _new_service()
    service.system_metrics = {
        "cpu": {"percent": 12.5},
        "updated_at": 123456,
    }
    service.service_status = {
        "edge-mosquitto": {
            "status": "healthy",
            "checks": {
                "tcp": {"status": "healthy", "latency_ms": 1.1},
                "container": {"status": "healthy", "state": "running"},
            },
            "latency_ms": 1.1,
            "container_state": "running",
            "timestamp": 200,
        },
        "edge-api": {
            "status": "degraded",
            "checks": {
                "http": {"status": "degraded", "latency_ms": 2.4},
                "container": {"status": "healthy", "state": "running"},
            },
            "latency_ms": 2.4,
            "container_state": "running",
            "timestamp": 150,
        },
    }
    
    payload = service._build_status_payload()
    
    assert payload["schema"] == service.status_schema
    assert payload["line"] == "L-test"
    assert payload["system"]["metrics"]["updated_at"] == 123456
    assert payload["services"][0]["name"] == "edge-api"
    assert payload["services"][1]["name"] == "edge-mosquitto"
    assert payload["services"][0]["status"] == "degraded"
    assert payload["services"][1]["status"] == "healthy"
    assert payload["control"]["self_heal"]["enabled"] is False


def test_merge_status_priority():
    service = _new_service()
    
    assert service._merge_status("unknown", "healthy") == "healthy"
    assert service._merge_status("healthy", "degraded") == "degraded"
    assert service._merge_status("degraded", "down") == "down"
    # 当候选状态优先级较低时，保持原状态
    assert service._merge_status("down", "healthy") == "down"


@pytest.mark.asyncio
async def test_http_health_check_success(monkeypatch):
    service = _new_service()
    
    class DummyResponse:
        status_code = 200
    
    class DummyClient:
        async def get(self, url):
            assert url == "http://edge-api/health"
            return DummyResponse()
    
    service.http_client = cast(AsyncClient, DummyClient())
    
    perf_calls = iter([100.0, 100.01])
    monkeypatch.setattr("monitor.main.time.perf_counter", lambda: next(perf_calls))
    
    result = await service._http_health_check("http://edge-api/health")
    
    assert result["status"] == "healthy"
    assert result["latency_ms"] == pytest.approx(10.0, rel=1e-3)
    assert result["status_code"] == 200


@pytest.mark.asyncio
async def test_http_health_check_without_client():
    service = _new_service()
    service.http_client = None
    
    result = await service._http_health_check("http://edge-api/health")
    assert result["status"] == "degraded"
    assert result["detail"] == "http_client_not_ready"


@pytest.mark.asyncio
async def test_http_health_check_records_json_parse_error(monkeypatch):
    service = _new_service()

    class DummyResponse:
        status_code = 503
        text = "not json"
        headers = {"content-type": "application/json"}

        def json(self):
            raise ValueError("bad json")

    class DummyClient:
        async def get(self, url):
            return DummyResponse()

    service.http_client = cast(AsyncClient, DummyClient())
    perf_calls = iter([100.0, 100.01])
    monkeypatch.setattr("monitor.main.time.perf_counter", lambda: next(perf_calls))

    result = await service._http_health_check("http://edge-api/health")

    assert result["status"] == "degraded"
    assert result["response_body"] == "not json"
    assert result["response_json_error"] == "bad json"
    assert result["detail"] == "http 503"


@pytest.mark.asyncio
async def test_http_health_check_records_body_read_error(monkeypatch):
    service = _new_service()

    class DummyResponse:
        status_code = 503

        @property
        def text(self):
            raise RuntimeError("body read failed")

    class DummyClient:
        async def get(self, url):
            return DummyResponse()

    service.http_client = cast(AsyncClient, DummyClient())
    perf_calls = iter([100.0, 100.01])
    monkeypatch.setattr("monitor.main.time.perf_counter", lambda: next(perf_calls))

    result = await service._http_health_check("http://edge-api/health")

    assert result["status"] == "degraded"
    assert result["response_body_error"] == "body read failed"
    assert result["detail"] == "http 503"


@pytest.mark.asyncio
async def test_tcp_health_check_success(monkeypatch):
    service = _new_service()
    
    class DummyWriter:
        def __init__(self):
            self.closed = False
            self.waited = False
        
        def close(self):
            self.closed = True
        
        async def wait_closed(self):
            self.waited = True
    
    async def fake_open_connection(host, port):
        assert host == "edge-mosquitto"
        assert port == 1883
        return None, DummyWriter()
    
    monkeypatch.setattr("monitor.main.asyncio.open_connection", fake_open_connection)
    perf_calls = iter([5.0, 5.005])
    monkeypatch.setattr("monitor.main.time.perf_counter", lambda: next(perf_calls))
    
    result = await service._tcp_health_check("edge-mosquitto", 1883)
    assert result["status"] == "healthy"
    assert result["latency_ms"] == pytest.approx(5.0, rel=1e-3)


@pytest.mark.asyncio
async def test_tcp_health_check_missing_host():
    service = _new_service()
    result = await service._tcp_health_check(None, None)
    assert result["status"] == "degraded"
    assert result["detail"] == "tcp_host_or_port_missing"


@pytest.mark.asyncio
async def test_manual_restart():
    service = _new_service()
    service.service_targets = [{"name": "edge-api", "container": "edge-api"}]

    with pytest.raises(HTTPException) as raised:
        await service.manual_restart("edge-api")

    assert raised.value.status_code == 503
    assert raised.value.detail["error"] == "host_container_mutation_disabled"


def test_set_self_heal_enabled():
    service = _new_service()
    service.service_targets = [{"name": "edge-api"}]

    resp = service.set_self_heal_enabled("edge-api", False)
    assert resp["enabled"] is False
    assert service.service_health_state["edge-api"]["self_heal_enabled"] is False


def test_alarm_transition_healthy_to_down_emits_firing_critical():
    service = _new_service()
    record = {"checks": {"http": {"detail": "container exited"}}}
    t = service._build_alarm_transition("edge-api", "healthy", "down", record)
    assert t is not None
    assert t["state"] == "firing"
    assert t["severity"] == "critical"
    assert t["alarm_key"] == "L-test:edge-api:health"
    assert t["message"] == "container exited"


def test_alarm_transition_healthy_to_degraded_emits_firing_warning():
    service = _new_service()
    t = service._build_alarm_transition("edge-api", "healthy", "degraded", {})
    assert t["state"] == "firing"
    assert t["severity"] == "warning"
    assert t["message"] == "edge-api health changed from healthy to degraded"


def test_alarm_transition_degraded_to_down_emits_firing_critical():
    service = _new_service()
    t = service._build_alarm_transition("edge-api", "degraded", "down", {})
    assert t["state"] == "firing"
    assert t["severity"] == "critical"


def test_alarm_transition_down_to_healthy_emits_resolved():
    service = _new_service()
    t = service._build_alarm_transition("edge-api", "down", "healthy", {})
    assert t["state"] == "resolved"
    assert t["severity"] is None


def test_alarm_transition_same_status_returns_none():
    service = _new_service()
    assert service._build_alarm_transition("edge-api", "degraded", "degraded", {}) is None
    assert service._build_alarm_transition("edge-api", "down", "down", {}) is None
    assert service._build_alarm_transition("edge-api", "healthy", "healthy", {}) is None


def test_alarm_transition_first_run_none_healthy_no_alarm():
    """first-run previous=None + current=healthy 不应发 alarm。"""
    service = _new_service()
    assert service._build_alarm_transition("edge-api", None, "healthy", {}) is None


def test_alarm_transition_first_run_none_down_emits_firing():
    """first-run previous=None + current=down 仍发 firing（启动即异常）。"""
    service = _new_service()
    t = service._build_alarm_transition("edge-api", None, "down", {})
    assert t is not None
    assert t["state"] == "firing"
    assert t["severity"] == "critical"
    assert t["message"] == "edge-api health changed from healthy to down"


def test_alarm_transition_malformed_checks_still_has_message():
    service = _new_service()
    t = service._build_alarm_transition("edge-api", "healthy", "down", {"checks": ["bad"]})
    assert t["state"] == "firing"
    assert t["message"] == "edge-api health changed from healthy to down"


def test_update_service_states_returns_transitions_tuple():
    """_update_service_states 返回 (snapshot, transitions)，previous_status 取自 mutation 前。"""
    service = _new_service()
    service.service_targets = [{"name": "edge-api"}]
    # 模拟上一轮 health 已是 healthy
    service.service_health_state = {"edge-api": {"status": "healthy", "consecutive_failures": 0}}

    aggregated = {"edge-api": {"status": "down", "checks": {"http": {"detail": "boom"}}}}
    snapshot, transitions = service._update_service_states(aggregated)

    assert "edge-api" in snapshot
    assert len(transitions) == 1
    assert transitions[0]["state"] == "firing"
    assert transitions[0]["severity"] == "critical"
    # 之后再跑同样的 down 不应该再生成 transition（previous 已是 down）
    snapshot2, transitions2 = service._update_service_states(aggregated)
    assert transitions2 == []


def test_update_service_states_does_not_touch_self_heal_fields():
    """transition 检测不应改变 failure count / cooldown / restart 任何字段。"""
    service = _new_service()
    service.service_targets = [{"name": "edge-api"}]
    aggregated = {"edge-api": {"status": "down"}}
    _, _ = service._update_service_states(aggregated)
    state = service.service_health_state["edge-api"]
    # consecutive_failures 由现有逻辑（不是 alarm hook）维护
    assert state["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_control_status_auto_paused_emits_visibility_alarm(monkeypatch):
    service = _new_service()
    _enable_control_probe(service)
    monkeypatch.setenv("ADMIN_API_KEYS", "admin-1")

    captured = {}

    class DummyClient:
        async def get(self, url, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = dict(headers or {})
            return _ControlStatusResponse(
                {
                    "success": True,
                    "data": {
                        "mode": "AUTO_PAUSED",
                        "auto_dispatch_status": "paused_requires_reauthorization",
                        "pause_reason": "PLC_NAK",
                        "requires_reauthorization": True,
                        "authority_snapshot": {"AUTHORITY_OWNER": "EDGE", "AUTHORITY_EPOCH": 3},
                        "transport": {"ready": True, "mqtt_connected": True, "signature_configured": True},
                        "last_command_result": None,
                        "last_auto_action": None,
                        "manual_command_entrypoint": "/api/v1/control/manual/set",
                        "auto_dispatch_entrypoint": "/api/v1/control/auto/dispatch",
                    },
                }
            )

    service.http_client = cast(AsyncClient, DummyClient())

    record = await service._check_control_status()
    transition = service._update_control_visibility_state(record)
    payload = service._build_status_payload()

    assert captured["url"].endswith("/api/v1/control/status")
    assert captured["headers"]["X-API-Key"] == "admin-1"
    assert record["status"] == "degraded"
    assert record["mode"] == "AUTO_PAUSED"
    assert record["pause_reason"] == "PLC_NAK"
    assert record["self_heal"]["enabled"] is False
    assert transition["state"] == "firing"
    assert transition["severity"] == "warning"
    assert transition["alarm_key"] == "L-test:control:visibility"
    assert transition["meta"]["mode"] == "AUTO_PAUSED"
    assert transition["meta"]["pause_reason"] == "PLC_NAK"
    assert payload["control"]["mode"] == "AUTO_PAUSED"
    assert payload["warnings"][0]["type"] == "CONTROL_VISIBILITY_DEGRADED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command_result", "expected_reason"),
    [
        (
            {"scope": "auto", "result": {"success": False, "status": "nak", "error": "control_nak"}},
            "CONTROL_COMMAND_NAK",
        ),
        (
            {"scope": "auto", "result": {"success": False, "status": "timeout", "error": "ack_timeout"}},
            "CONTROL_COMMAND_TIMEOUT",
        ),
        (
            {"scope": "manual", "result": {"success": False, "status": "error", "error": "mqtt_publish_failed"}},
            "CONTROL_COMMAND_ERROR",
        ),
    ],
)
async def test_control_status_command_failures_are_degraded(command_result, expected_reason):
    service = _new_service()
    _enable_control_probe(service)

    class DummyClient:
        async def get(self, url, headers=None, timeout=None):
            return _ControlStatusResponse(
                {
                    "success": True,
                    "data": {
                        "mode": "AUTO",
                        "auto_dispatch_status": "ready",
                        "pause_reason": None,
                        "requires_reauthorization": False,
                        "authority_snapshot": {"AUTHORITY_OWNER": "EDGE", "AUTHORITY_EPOCH": 3},
                        "transport": {"ready": True, "mqtt_connected": True, "signature_configured": True},
                        "last_command_result": command_result,
                    },
                }
            )

    service.http_client = cast(AsyncClient, DummyClient())

    record = await service._check_control_status()

    assert record["status"] == "degraded"
    assert expected_reason in record["reasons"]
    assert record["last_command_result"]["problem"] is True
    assert record["self_heal"]["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "dispatch_status"),
    [
        ("MANUAL", "manual_stopped"),
        ("AUTO", "ready"),
    ],
)
async def test_control_status_normal_mode_resolves_stale_command_failure_without_self_heal(mode, dispatch_status):
    service = _new_service()
    _enable_control_probe(service)
    service.control_visibility_state = {"status": "degraded"}
    service.service_health_state = {"edge-api": {"status": "healthy", "consecutive_failures": 0}}

    class DummyClient:
        async def get(self, url, headers=None, timeout=None):
            return _ControlStatusResponse(
                {
                    "success": True,
                    "data": {
                        "mode": mode,
                        "auto_dispatch_status": dispatch_status,
                        "pause_reason": None,
                        "requires_reauthorization": False,
                        "last_transition": {"from": "AUTO_PAUSED", "to": mode, "at": "2026-06-30T00:01:00+00:00"},
                        "authority_snapshot": {"AUTHORITY_OWNER": "EDGE", "AUTHORITY_EPOCH": 3},
                        "transport": {"ready": True, "mqtt_connected": True, "signature_configured": True},
                        "last_command_result": {
                            "scope": "auto",
                            "recorded_at": "2026-06-30T00:00:00+00:00",
                            "result": {"success": False, "status": "nak", "error": "control_nak"},
                        },
                    },
                }
            )

    service.http_client = cast(AsyncClient, DummyClient())

    record = await service._check_control_status()
    transition = service._update_control_visibility_state(record)

    assert record["status"] == "healthy"
    assert record["detail"] == "control_status_ok"
    assert record["last_command_result"]["problem"] is True
    assert transition["state"] == "resolved"
    assert transition["severity"] is None
    assert service.control_status["self_heal"]["enabled"] is False
    assert service.service_health_state["edge-api"]["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_control_status_unreachable_is_down_not_service_self_heal():
    service = _new_service()
    _enable_control_probe(service)
    service.service_health_state = {}

    class DummyClient:
        async def get(self, url, headers=None, timeout=None):
            raise httpx.ConnectError("connect refused")

    service.http_client = cast(AsyncClient, DummyClient())

    record = await service._check_control_status()
    transition = service._update_control_visibility_state(record)

    assert record["status"] == "down"
    assert record["detail"] == "control_status_unreachable"
    assert transition["state"] == "firing"
    assert transition["severity"] == "critical"
    assert service.control_status["self_heal"]["enabled"] is False
    assert service.service_health_state == {}


@pytest.mark.asyncio
async def test_emit_alarm_transitions_skips_without_config():
    service = _new_service()
    service.alarm_ingest_url = ""
    service.alarm_ingest_token = ""
    # 无 url/token 时应静默跳过，不抛
    await service._emit_alarm_transitions([
        {
            "service": "edge-api",
            "alarm_key": "L-test:edge-api:health",
            "state": "firing",
            "severity": "critical",
            "message": "x",
            "previous_status": "healthy",
            "current_status": "down",
        }
    ])


@pytest.mark.asyncio
async def test_emit_alarm_transitions_posts_to_ingest_endpoint():
    service = _new_service()
    service.alarm_ingest_url = "http://edge-api:8000/api/v1/alarms/internal/ingest"
    service.alarm_ingest_token = "tk-monitor"

    posted = []

    class DummyClient:
        async def post(self, url, json, headers, timeout):
            posted.append({"url": url, "json": json, "headers": dict(headers), "timeout": timeout})

            class _R:
                status_code = 200
            return _R()

    service.http_client = cast(AsyncClient, DummyClient())

    transitions = [
        {
            "service": "edge-api",
            "alarm_key": "L-test:edge-api:health",
            "state": "firing",
            "severity": "critical",
            "message": "boom",
            "previous_status": "healthy",
            "current_status": "down",
        },
        {
            "service": "edge-daq",
            "alarm_key": "L-test:edge-daq:health",
            "state": "resolved",
            "severity": None,
            "message": None,
            "previous_status": "down",
            "current_status": "healthy",
        },
    ]
    await service._emit_alarm_transitions(transitions)

    assert len(posted) == 2
    first = posted[0]
    assert first["url"] == "http://edge-api:8000/api/v1/alarms/internal/ingest"
    assert first["headers"]["X-API-Key"] == "tk-monitor"
    assert first["json"]["alarm_key"] == "L-test:edge-api:health"
    assert first["json"]["state"] == "firing"
    assert first["json"]["severity"] == "critical"
    assert first["json"]["source_service"] == "edge-monitor"
    assert first["json"]["line_id"] == "L-test"
    assert first["json"]["message"] == "boom"
    assert first["json"]["meta"]["previous_status"] == "healthy"
    assert first["json"]["meta"]["current_status"] == "down"

    second = posted[1]
    assert second["json"]["state"] == "resolved"
    assert "severity" not in second["json"]  # severity is None → 不传
    assert "message" not in second["json"]   # message is None → 不传


@pytest.mark.asyncio
async def test_emit_alarm_transitions_single_failure_does_not_block_others(monkeypatch):
    service = _new_service()
    service.alarm_ingest_url = "http://edge-api:8000/api/v1/alarms/internal/ingest"
    service.alarm_ingest_token = "tk-monitor"

    calls = []

    class DummyClient:
        async def post(self, url, json, headers, timeout):
            calls.append(json["alarm_key"])
            if json["alarm_key"].endswith("edge-api:health"):
                raise RuntimeError("connect refused")

            class _R:
                status_code = 200
            return _R()

    service.http_client = cast(AsyncClient, DummyClient())

    transitions = [
        {
            "service": "edge-api",
            "alarm_key": "L-test:edge-api:health",
            "state": "firing",
            "severity": "critical",
            "message": None,
            "previous_status": "healthy",
            "current_status": "down",
        },
        {
            "service": "edge-daq",
            "alarm_key": "L-test:edge-daq:health",
            "state": "firing",
            "severity": "warning",
            "message": None,
            "previous_status": "healthy",
            "current_status": "degraded",
        },
    ]
    # 不应抛
    await service._emit_alarm_transitions(transitions)
    assert calls == ["L-test:edge-api:health", "L-test:edge-daq:health"]
