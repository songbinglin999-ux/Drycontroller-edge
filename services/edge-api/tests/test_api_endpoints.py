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
from app.api.v1.system import router as system_router, get_system_status_service
from app.api.v1.control import router as control_router, get_control_recommendation_service
from app.api.v1.alarms import router as alarm_router, get_alarm_service


class DummyAISvc:
    async def get_lab_samples(self, **kwargs):
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
                "recommendations": {"DRYER1.FAN.SPEED": 35.5},
            },
        }


class DummyAlarmSvc:
    async def get_active_alarms(self, **kwargs):
        return {"success": True, "count": 1, "data": [{"name": "edge-api", "status": "down"}]}

    async def get_alarm_history(self, **kwargs):
        return {"success": True, "count": 0, "data": []}


app = FastAPI()
app.include_router(ai_router, prefix="/api/v1/ai")
app.include_router(system_router, prefix="/api/v1/system")
app.include_router(control_router, prefix="/api/v1/control")
app.include_router(alarm_router, prefix="/api/v1/alarms")
app.dependency_overrides[get_ai_softsensor_service] = lambda: DummyAISvc()
app.dependency_overrides[get_system_status_service] = lambda: DummySystemSvc()
app.dependency_overrides[get_control_recommendation_service] = lambda: DummyControlSvc()
app.dependency_overrides[get_alarm_service] = lambda: DummyAlarmSvc()
client = TestClient(app)


def test_ai_lab_samples_endpoint():
    resp = client.get("/api/v1/ai/lab/samples?start=-1d&limit=1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["data"][0]["line_id"] == "L1"


def test_system_metric_trend_endpoint():
    resp = client.get("/api/v1/system/metrics/trend?field=cpu_percent")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


def test_control_recommend_endpoint():
    resp = client.get("/api/v1/control/recommend")
    assert resp.status_code == 200
    assert "recommendations" in resp.json()["data"]


def test_alarm_active_endpoint():
    resp = client.get("/api/v1/alarms/active")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1

