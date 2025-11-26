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

from monitor.main import MonitorService


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