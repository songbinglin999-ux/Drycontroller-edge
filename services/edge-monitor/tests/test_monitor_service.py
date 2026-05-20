import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from docker.client import DockerClient
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

from typing import cast

from monitor.main import MonitorService
from monitor.self_heal import SelfHealExecutionResult, SelfHealExecutor


def _new_service():
    svc = MonitorService()
    svc.line_id = "L-test"
    return svc


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


def test_container_health_check_running():
    service = _new_service()
    
    class DummyContainers:
        def get(self, name):
            assert name == "edge-api"
            return SimpleNamespace(status="running")
    
    class DummyDocker:
        containers = DummyContainers()
    
    service.docker_client = cast(DockerClient, DummyDocker())
    result = service._container_health_check("edge-api")
    assert result["status"] == "healthy"
    assert result["state"] == "running"


def test_container_health_check_without_docker():
    service = _new_service()
    service.docker_client = None
    result = service._container_health_check("edge-api")
    assert result["status"] == "degraded"
    assert result["detail"] == "docker_client_not_ready"


@pytest.mark.asyncio
async def test_manual_restart(monkeypatch):
    service = _new_service()
    service.service_targets = [{"name": "edge-api", "container": "edge-api"}]

    class DummyExecutor(SelfHealExecutor):
        async def restart_container(self, container_name, timeout=15):
            assert container_name == "edge-api"
            return SelfHealExecutionResult(success=True)

    service.self_heal_manager.executor = cast(SelfHealExecutor, DummyExecutor())
    service.self_heal_auditor = None

    result = await service.manual_restart("edge-api")
    assert result["success"] is True


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