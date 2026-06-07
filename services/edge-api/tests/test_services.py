"""
edge-api 服务层测试
"""

import asyncio
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, cast

import pytest

PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]   # edge-api/
ROOT = PATH.parents[3]           # Drycontroller-edge/ (项目根，含 common/)

for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

from app.services.realtime_data import RealtimeDataService  # noqa: E402
from app.services.ai_softsensor import AISoftsensorService  # noqa: E402
from app.services.system_status import SystemStatusService  # noqa: E402
from app.services.event_bus import EventBus  # noqa: E402
from app.services.control_recommendation import (  # noqa: E402
    ControlRecommendationService,
)
from app.services.alarm import AlarmService  # noqa: E402
from app.services.influx import InfluxQueryService  # noqa: E402
from app.services.monitor_config import MonitorConfigService  # noqa: E402


class DummyInflux:
    async def latest_points(self, tags: List[str], lookback: str) -> List[Dict[str, Any]]:
        return [{"tag": "A.TAG", "value": 1.0}, {"tag": "B.TAG", "value": 2.0}]

    async def history_points(
        self,
        tag: str,
        period: str,
        window: str,
        aggregation: str,
    ) -> List[Dict[str, Any]]:
        return [{"time": "2025-01-01T00:00:00Z", "value": 12.3}]

    async def training_samples(
        self,
        measurement: str,
        start: str,
        stop: str,
        line: Optional[str],
        require_lab: bool,
    ) -> List[Dict[str, Any]]:
        row = {
            "time": "2025-01-01T00:00:00Z",
            "line_id": line or "L1",
            "moisture_predicted": 12.4,
            "moisture_lab": 12.8,
            "feature_window_start": 1700000000,
        }
        if require_lab:
            return [row]
        row_no_lab = dict(row)
        row_no_lab.pop("moisture_lab", None)
        return [row, row_no_lab]

    async def system_metric_trend(
        self,
        measurement: str,
        field: str,
        period: str,
        window: str,
        line: Optional[str],
    ) -> List[Dict[str, Any]]:
        return [{"time": "2025-01-01T00:00:00Z", "value": 42.0}]

    async def active_service_alarms(
        self,
        measurement: str,
        period: str,
        severity: Optional[str],
        include_history: bool = False,
    ) -> List[Dict[str, Any]]:
        alarm = {"name": "edge-api", "status": "down", "time": "2025-01-01T00:00:00Z"}
        if include_history:
            return [alarm, dict(alarm)]
        return [alarm]


@pytest.mark.asyncio
async def test_realtime_data_service_latest_payload(monkeypatch):
    monkeypatch.setattr(
        "app.services.realtime_data.get_all_tag_names",
        lambda: ["A.TAG", "B.TAG", "C.TAG"],
    )
    service = RealtimeDataService(
        influx_service=cast(InfluxQueryService, DummyInflux()),
    )

    result = await service.get_latest_payload(None, lookback="5m")

    assert result["success"] is True
    assert result["count"] == 2
    assert "C.TAG" in result["missing"]


def test_realtime_data_service_tags_payload(monkeypatch):
    monkeypatch.setattr(
        "app.services.realtime_data.get_all_tag_metadata",
        lambda: [
            {
                "name": "FTT1",
                "signal_type": "AI",
                "type": "float",
                "unit": "℃",
                "description": "主机1风温",
            }
        ],
    )
    service = RealtimeDataService(
        influx_service=cast(InfluxQueryService, DummyInflux()),
    )

    result = service.get_tags_payload()

    assert result["success"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["tags"][0]["name"] == "FTT1"
    assert result["data"]["tags"][0]["unit"] == "℃"
    assert result["data"]["tags"][0]["description"] == "主机1风温"


@pytest.mark.asyncio
async def test_realtime_data_service_history_payload(monkeypatch):
    monkeypatch.setattr(
        "app.services.realtime_data.get_all_tag_names",
        lambda: ["MOISTURE.PREDICTED"],
    )
    service = RealtimeDataService(
        influx_service=cast(InfluxQueryService, DummyInflux()),
    )

    result = await service.get_history_payload(
        tag="MOISTURE.PREDICTED",
        period="1h",
        window="1m",
        aggregation="mean",
    )

    assert result["success"] is True
    assert result["data"]["tag"] == "MOISTURE.PREDICTED"
    assert len(result["data"]["points"]) == 1


class DummyRealtimeForAI(RealtimeDataService):
    async def get_history_payload(self, tag, period, window, aggregation):
        return {
            "success": True,
            "data": {"tag": tag, "points": [{"time": "2025-01-01T00:00Z", "value": 12.0}]},
            "count": 1,
            "timestamp": 1,
        }


class DummyStream:
    def __init__(self, payload: Optional[dict] = None) -> None:
        self.payload = payload or {"line": "L1", "value": 12.4}

    def get_latest(self, stream_name: str, line: Optional[str] = None):
        if stream_name != "ai":
            return None
        current = dict(self.payload)
        if line:
            current["line"] = line
        return current


def test_ai_softsensor_current(monkeypatch):
    dummy_stream = DummyStream()
    monkeypatch.setattr("app.services.ai_softsensor.ws_stream", dummy_stream)
    dummy_influx = cast(InfluxQueryService, DummyInflux())
    service = AISoftsensorService(
        realtime_service=DummyRealtimeForAI(),
        influx_service=dummy_influx,
    )

    payload = service.get_current_moisture("L2")
    assert payload is not None

    assert payload["success"] is True
    assert payload["data"]["line"] == "L2"


@pytest.mark.asyncio
async def test_ai_softsensor_history(monkeypatch):
    dummy_stream = DummyStream()
    monkeypatch.setattr("app.services.ai_softsensor.ws_stream", dummy_stream)
    dummy_influx = cast(InfluxQueryService, DummyInflux())
    service = AISoftsensorService(
        realtime_service=DummyRealtimeForAI(),
        influx_service=dummy_influx,
    )

    payload = await service.get_moisture_history("1h", "5m", "mean")

    assert payload["data"]["tag"] == AISoftsensorService.PREDICTED_TAG
    assert payload["count"] == 1


def test_ai_softsensor_model_info(tmp_path, monkeypatch):
    config_path = tmp_path / "ai-config.yaml"
    config_path.write_text(
        "softsensor:\n"
        "  model_name: moisture-predictor\n"
        "  model_version: v1.0.0\n"
        "  input_features:\n"
        "    - TAG1\n"
        "optimizer:\n"
        "  model_name: control-optimizer\n"
        "  model_version: v0.1.0\n",
        encoding="utf-8",
    )
    dummy_stream = DummyStream()
    monkeypatch.setattr("app.services.ai_softsensor.ws_stream", dummy_stream)

    dummy_influx = cast(InfluxQueryService, DummyInflux())
    service = AISoftsensorService(
        realtime_service=DummyRealtimeForAI(),
        influx_service=dummy_influx,
        config_path=config_path,
    )

    payload = service.get_models_info()

    assert payload["data"]["softsensor"]["model_name"] == "moisture-predictor"
    assert payload["data"]["optimizer"]["model_version"] == "v0.1.0"


@pytest.mark.asyncio
async def test_ai_softsensor_lab_samples(monkeypatch):
    dummy_stream = DummyStream()
    monkeypatch.setattr("app.services.ai_softsensor.ws_stream", dummy_stream)
    dummy_influx = cast(InfluxQueryService, DummyInflux())
    service = AISoftsensorService(
        realtime_service=DummyRealtimeForAI(),
        influx_service=dummy_influx,
    )

    payload = await service.get_lab_samples(line="L2", limit=1)

    assert payload["count"] == 1
    assert payload["data"][0]["line_id"] == "L2"


def test_system_status_service(monkeypatch):
    sample_payload = {
        "line": "L1",
        "services": [{"name": "edge-api", "status": "healthy"}],
        "system": {"metrics": {"cpu": {"percent": 12.5}}},
    }

    class DummySystemStream:
        def get_latest(self, stream_name: str, line: Optional[str] = None):
            assert stream_name == "system"
            return sample_payload

    monkeypatch.setattr("app.services.system_status.ws_stream", DummySystemStream())

    dummy_influx = cast(InfluxQueryService, DummyInflux())
    service = SystemStatusService(influx_service=dummy_influx)

    status_payload = service.get_status_payload("L1")
    metrics_payload = service.get_metrics_payload("L1")
    assert status_payload is not None
    assert metrics_payload is not None


def test_monitor_config_service_patch(tmp_path):
    config_path = tmp_path / "monitor.yaml"
    config_path.write_text(
        "services:\n"
        "  interval: 20\n"
        "  targets:\n"
        "    - name: edge-api\n"
        "      self_heal:\n"
        "        enabled: true\n",
        encoding="utf-8",
    )

    service = MonitorConfigService(str(config_path))
    updated = service.patch(
        {
            "failure_threshold": 5,
            "self_heal_enabled": False,
            "targets": [
                {"name": "edge-api", "self_heal": {"enabled": False, "max_restart_per_hour": 2}}
            ],
        }
    )

    assert updated["services"]["failure_threshold"] == 5
    assert updated["services"]["self_heal"]["enabled"] is False
    edge_api = next(item for item in updated["services"]["targets"] if item["name"] == "edge-api")
    assert edge_api["self_heal"]["enabled"] is False
    assert edge_api["self_heal"]["max_restart_per_hour"] == 2


@pytest.mark.asyncio
async def test_system_metric_trend(monkeypatch):
    dummy_influx = cast(InfluxQueryService, DummyInflux())
    service = SystemStatusService(influx_service=dummy_influx)

    payload = await service.get_metric_trend(
        field="cpu_percent",
        period="6h",
        window="5m",
        line="L1",
    )

    assert payload["success"] is True
    assert payload["count"] == 1


class DummyRealtimeControl:
    async def get_latest_payload(self, requested_tags, lookback):
        return {
            "success": True,
            "data": [
                {"tag": "MOISTURE.PREDICTED", "value": 13.5},
                {"tag": "MT1", "value": 14.2},
                {"tag": "I_M3", "value": 42.0},
                {"tag": "M3", "value": 1},
                {"tag": "I_M5", "value": 15.0},
                {"tag": "M5", "value": 1},
                {"tag": "VF1", "value": 35.0},
                {"tag": "VFM1", "value": 30.0},
            ],
            "missing": [],
        }

    async def get_history_payload(self, tag, period, window, aggregation):
        values = [12.0, 16.0, 42.0] if tag == "I_M3" else [14.0, 15.0, 15.0]
        points = [
            {"time": "2026-06-08T00:00:00Z", "value": values[0]},
            {"time": "2026-06-08T00:02:30Z", "value": values[1]},
            {"time": "2026-06-08T00:05:00Z", "value": values[2]},
        ]
        return {
            "success": True,
            "data": {"tag": tag, "points": points},
            "count": len(points),
            "timestamp": 1,
        }


class DummyRealtimeControlNoData:
    async def get_latest_payload(self, requested_tags, lookback):
        return {
            "success": True,
            "data": [],
            "missing": list(requested_tags),
        }

    async def get_history_payload(self, tag, period, window, aggregation):
        return {
            "success": True,
            "data": {"tag": tag, "points": []},
            "count": 0,
            "timestamp": 1,
        }


@pytest.mark.asyncio
async def test_control_recommendations_use_new_diagnosis_schema():
    realtime_stub = cast(RealtimeDataService, DummyRealtimeControl())
    service = ControlRecommendationService(realtime_service=realtime_stub)

    payload = await service.get_recommendations(line="L1", lookback="5m")

    assert payload["success"] is True
    data = payload["data"]
    assert data["schema_version"] == ControlRecommendationService.SCHEMA_VERSION
    assert data["actionable"] is False
    assert "recommendations" not in data
    assert "DRYER1" not in str(data)

    diagnosis = data["diagnosis"][0]
    assert diagnosis["severity"] == "warning"
    assert diagnosis["probable_cause"] == "load_rising_or_possible_downstream_blockage"
    assert diagnosis["evidence"][0]["tag"] == "I_M3"
    assert data["suggestions"][0]["actionable"] is False
    assert "P0-SAFETY" in data["suggestions"][0]["blocked_by"]


@pytest.mark.asyncio
async def test_control_recommendations_mark_missing_when_no_data():
    realtime_stub = cast(RealtimeDataService, DummyRealtimeControlNoData())
    service = ControlRecommendationService(realtime_service=realtime_stub)

    payload = await service.get_recommendations(line="L1", lookback="5m")

    data = payload["data"]
    assert data["freshness"] == "missing"
    assert data["diagnosis"][0]["freshness"] == "missing"
    assert data["diagnosis"][0]["evidence"] == []
    assert data["suggestions"][0]["freshness"] == "missing"


@pytest.mark.asyncio
async def test_alarm_service_active_history():
    dummy_influx = cast(InfluxQueryService, DummyInflux())
    service = AlarmService(influx_service=dummy_influx)

    active = await service.get_active_alarms(period="10m", severity=None)
    history = await service.get_alarm_history(period="24h")

    assert active["count"] == len(active["data"])
    assert history["count"] == len(history["data"])


class DummyMQTTAdapter:
    def __init__(self):
        self.callbacks: Dict[str, Callable[[str, str], None]] = {}
        self.subscriptions: List[str] = []

    def subscribe(self, topic: str, qos: int = 1):
        self.subscriptions.append(topic)

    def register_callback(self, topic_pattern: str, callback: Callable[[str, str], None]):
        self.callbacks[topic_pattern] = callback
        return callback

    def unregister_callback(self, topic_pattern: str, callback: Callable[[str, str], None]):
        self.callbacks.pop(topic_pattern, None)


@pytest.mark.asyncio
async def test_event_bus_publish_and_subscribe():
    bus = EventBus(mqtt_adapter=DummyMQTTAdapter(), default_patterns=[])
    await bus.start()
    queue = bus.subscribe("demo/#")

    bus.publish("demo/topic", {"foo": "bar"})

    event = await asyncio.wait_for(queue.get(), timeout=0.1)
    assert event["data"]["foo"] == "bar"
    await bus.stop()


@pytest.mark.asyncio
async def test_event_bus_mqtt_dispatch():
    adapter = DummyMQTTAdapter()
    bus = EventBus(mqtt_adapter=adapter, default_patterns=[])
    await bus.start()
    queue = bus.subscribe("ai/#")

    callback = adapter.callbacks["ai/#"]
    callback("ai/L1/predict", '{"line":"L1"}')

    event = await asyncio.wait_for(queue.get(), timeout=0.1)
    assert event["topic"] == "ai/L1/predict"
    assert event["data"]["line"] == "L1"
    await bus.stop()
