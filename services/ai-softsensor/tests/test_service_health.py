"""
ai-softsensor 服务健康与就绪判定测试
"""

import os
import sys
import importlib.util
import types
from dataclasses import dataclass
from typing import Any, cast
from pathlib import Path


PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]

LOG_ROOT = Path("/tmp/drycontroller-logs")
LOG_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOG_ROOT", str(LOG_ROOT))

for entry in (str(SERVICE_ROOT), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

SRC_ROOT = SERVICE_ROOT / "src"
pkg = types.ModuleType("src")
pkg.__path__ = [str(SRC_ROOT)]
sys.modules["src"] = pkg


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module spec: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


infer_mod = _load_module("ai_softsensor_infer", SRC_ROOT / "infer.py")
main_mod = _load_module("ai_softsensor_main", SRC_ROOT / "main.py")
SoftSensorModel = infer_mod.SoftSensorModel
SoftSensorService = main_mod.SoftSensorService


@dataclass
class DummyMQTT:
    connected: bool = False
    dq_ready: bool = False

    def is_dq_ready(self) -> bool:
        return self.dq_ready

    def dq_snapshot(self):
        return {"mqtt_connected": self.connected}


def test_ready_payload_success():
    service = SoftSensorService()
    service.running = True
    service.model = SoftSensorModel({"input_features": []})
    service.mqtt_handler = cast(Any, DummyMQTT(connected=True, dq_ready=True))

    payload = service._ready_payload()

    assert payload["ready"] is True
    assert payload["reason"] is None


def test_ready_payload_reason_when_not_running():
    service = SoftSensorService()
    service.running = False
    service.model = SoftSensorModel({"input_features": []})
    service.mqtt_handler = cast(Any, DummyMQTT(connected=True, dq_ready=True))

    payload = service._ready_payload()

    assert payload["ready"] is False
    assert payload["reason"] == "SERVICE_STARTING"


def test_ready_payload_reason_when_model_missing():
    service = SoftSensorService()
    service.running = True
    service.model = None
    service.mqtt_handler = cast(Any, DummyMQTT(connected=True, dq_ready=True))

    payload = service._ready_payload()

    assert payload["ready"] is False
    assert payload["reason"] == "MODEL_NOT_READY"


def test_ready_payload_reason_when_mqtt_offline():
    service = SoftSensorService()
    service.running = True
    service.model = SoftSensorModel({"input_features": []})
    service.mqtt_handler = cast(Any, DummyMQTT(connected=False, dq_ready=True))

    payload = service._ready_payload()

    assert payload["ready"] is False
    assert payload["reason"] == "MQTT_OFFLINE"


def test_ready_payload_reason_when_dq_not_ready():
    service = SoftSensorService()
    service.running = True
    service.model = SoftSensorModel({"input_features": []})
    service.mqtt_handler = cast(Any, DummyMQTT(connected=True, dq_ready=False))

    payload = service._ready_payload()

    assert payload["ready"] is False
    assert payload["reason"] == "DQ_NOT_READY"

