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
from app.services.control_core.models import (  # noqa: E402
    ACTIVE_TARGET_TAGS,
    CONTROL_BASIS_BLOCK,
    REQUIRED_SNAPSHOT_TAGS,
    SCHEMA_VERSION,
)
from app.services.control_orchestrator import ControlOrchestrator  # noqa: E402
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


async def _noop_write_audit(*_args, **_kwargs):
    return None


def _control_orchestrator(realtime_stub: RealtimeDataService, monkeypatch) -> ControlOrchestrator:
    monkeypatch.setattr("app.services.control_orchestrator.write_audit", _noop_write_audit)
    return ControlOrchestrator(realtime_service=realtime_stub)


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


def _real_control_values(**overrides):
    values = {tag: 0 for tag in REQUIRED_SNAPSHOT_TAGS}
    values.update({tag: 30.0 for tag in ACTIVE_TARGET_TAGS})
    values.update(
        {
            "AUTHORITY_OWNER": "EDGE",
            "AUTHORITY_EPOCH": 1,
            "AUTH_TRANSITION_STATE": 0,
            "HMI_DIRECT_ENABLE": 0,
            "FTT1_T": 70.0,
            "FTT2_T": 70.0,
            "FTT3_T": 70.0,
            "TT1_T": 82.0,
            "TT2_T": 80.0,
            "MT1_H": 13.0,
            "VFM1_ACTIVE_FREQ_TARGET": 30.0,
            "VFM2_ACTIVE_FREQ_TARGET": 30.0,
            "VFM3_ACTIVE_FREQ_TARGET": 30.0,
            "VF1_ACTIVE_FREQ_TARGET": 35.0,
            "VF2_ACTIVE_FREQ_TARGET": 35.0,
            "VF3_ACTIVE_FREQ_TARGET": 35.0,
        }
    )
    values.update(overrides)
    return values


def _rows_for_requested(requested_tags, values):
    return [{"tag": tag, "value": values[tag]} for tag in requested_tags if tag in values]


class DummyRealtimeControl:
    async def get_latest_payload(self, requested_tags, lookback):
        values = _real_control_values(
            M3_I=42.0,
            M5_I=15.0,
            VF1_ACTIVE_FREQ_TARGET=35.0,
            VFM1_ACTIVE_FREQ_TARGET=30.0,
        )
        return {
            "success": True,
            "data": _rows_for_requested(requested_tags, values),
            "missing": [],
        }

    async def get_history_payload(self, tag, period, window, aggregation):
        values = [12.0, 16.0, 42.0] if tag == "M3_I" else [14.0, 15.0, 15.0]
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


class DummyRealtimeConveyorBlockage:
    async def get_latest_payload(self, requested_tags, lookback):
        values = _real_control_values(
            M11_I=32.0,
            M12_I=38.0,
            M13_I=17.0,
        )
        return {
            "success": True,
            "data": _rows_for_requested(requested_tags, values),
            "missing": [],
        }

    async def get_history_payload(self, tag, period, window, aggregation):
        values_by_tag = {
            "M11_I": [12.0, 13.0, 32.0],
            "M12_I": [18.0, 20.0, 38.0],
            "M13_I": [16.0, 17.0, 17.0],
        }
        values = values_by_tag.get(tag, [10.0, 10.0, 10.0])
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


class DummyRealtimeConveyorEmptyRun:
    async def get_latest_payload(self, requested_tags, lookback):
        values = _real_control_values(
            M1_I=4.0,
            M2_I=5.0,
        )
        return {
            "success": True,
            "data": _rows_for_requested(requested_tags, values),
            "missing": [],
        }

    async def get_history_payload(self, tag, period, window, aggregation):
        values_by_tag = {
            "M1_I": [20.0, 21.0, 4.0],
            "M2_I": [18.0, 17.0, 5.0],
        }
        values = values_by_tag.get(tag, [10.0, 10.0, 10.0])
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


@pytest.mark.asyncio
async def test_control_recommendations_use_new_diagnosis_schema(monkeypatch):
    realtime_stub = cast(RealtimeDataService, DummyRealtimeControl())
    orchestrator = _control_orchestrator(realtime_stub, monkeypatch)

    payload = await orchestrator.get_recommendations(line="L1", lookback="5m")

    assert payload["success"] is True
    data = payload["data"]
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["freshness"]["status"] == "fresh"
    assert data["freshness"]["missing_tags"] == []
    assert data["requires_confirmation"] is False
    assert data["production_requires_confirmation"] is False
    assert data["operation_modes"] == ["MANUAL", "AUTO"]
    assert data["auto_pause_state"] == "AUTO_PAUSED"
    assert data["auto_pause_is_operator_mode"] is False
    assert data["recommendation_confirmation_scope"] == "advisory_only"
    assert "recommendations" in data
    assert "diagnosis" not in data
    assert "DRYER1" not in str(data)

    recommendation = next(
        item for item in data["recommendations"]
        if item["diagnosis_code"] == "current_trend_high_load"
    )
    assert recommendation["level"] == "warning"
    assert recommendation["evidence"][0]["tag"] == "M3_I"
    assert recommendation["requires_confirmation"] is False
    assert recommendation["actionable"] is False
    assert CONTROL_BASIS_BLOCK in recommendation["blocked_by"]
    assert "DATA_MISSING_OR_STALE" not in recommendation["blocked_by"]


@pytest.mark.asyncio
async def test_control_recommendations_detect_downstream_blockage(monkeypatch):
    realtime_stub = cast(RealtimeDataService, DummyRealtimeConveyorBlockage())
    orchestrator = _control_orchestrator(realtime_stub, monkeypatch)

    payload = await orchestrator.get_recommendations(line="L1", lookback="5m")

    recommendation = next(
        item for item in payload["data"]["recommendations"]
        if item["diagnosis_code"] == "current_trend_high_load"
    )
    assert recommendation["id"] == "rec_l1_current_trend_high_load_m11_i"
    assert recommendation["level"] == "warning"
    assert recommendation["actionable"] is False
    assert recommendation["evidence"][0]["tag"] == "M11_I"
    assert recommendation["evidence"][0]["signal"] == "current"
    assert recommendation["suggested_actions"][0]["target_softpoint"] == "VFM3_FREQ_SP"
    assert "M11" not in {action["target_softpoint"] for action in recommendation["suggested_actions"]}


@pytest.mark.asyncio
async def test_control_recommendations_do_not_fabricate_empty_run_without_run_feedback(monkeypatch):
    realtime_stub = cast(RealtimeDataService, DummyRealtimeConveyorEmptyRun())
    orchestrator = _control_orchestrator(realtime_stub, monkeypatch)

    payload = await orchestrator.get_recommendations(line="L1", lookback="5m")

    assert all(
        item["diagnosis_code"] != "current_trend_empty_run"
        for item in payload["data"]["recommendations"]
    )


@pytest.mark.asyncio
async def test_control_recommendations_mark_missing_when_no_data(monkeypatch):
    realtime_stub = cast(RealtimeDataService, DummyRealtimeControlNoData())
    orchestrator = _control_orchestrator(realtime_stub, monkeypatch)

    payload = await orchestrator.get_recommendations(line="L1", lookback="5m")

    data = payload["data"]
    assert data["freshness"]["status"] == "missing"
    assert data["recommendations"][0]["diagnosis_code"] == "data_degraded"
    assert data["recommendations"][0]["actionable"] is False
    assert "DATA_MISSING_OR_STALE" in data["recommendations"][0]["blocked_by"]


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


def test_mqtt_manager_isolates_unexpected_callback_failures():
    from app.core.mqtt_client import MQTTManager

    manager = MQTTManager()
    calls: List[str] = []

    def bad_callback(_topic: str, _payload: str) -> None:
        raise AssertionError("subscriber invariant failed")

    def good_callback(topic: str, payload: str) -> None:
        calls.append(f"{topic}:{payload}")

    manager.message_callbacks["ai/#"].extend([bad_callback, good_callback])

    class Msg:
        topic = "ai/L1/predict"
        payload = b'{"line":"L1"}'

    manager._on_message(None, None, Msg())

    assert calls == ['ai/L1/predict:{"line":"L1"}']
