import json
import os
import sys
from pathlib import Path
from typing import Any, List

PATH = Path(__file__).resolve()
ROOT = PATH.parents[3]
SERVICE_ROOT = PATH.parents[1]
LOCAL_LOG_ROOT = ROOT / "logs"
LOCAL_LOG_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOG_ROOT", str(LOCAL_LOG_ROOT))
for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

from monitor.audit import SelfHealAuditor
from monitor.self_heal import SelfHealDecision, SelfHealExecutionResult, SelfHealOutcome


class DummyRecorder:
    def __init__(self):
        self.calls: List[Any] = []

    def record_event(self, name, *, tags=None, fields=None):
        self.calls.append((name, tags or {}, fields or {}))


class DummyMQTT:
    def __init__(self):
        self.topic = None
        self.payload = None
        self.qos = None
        self.retain = None

    def publish(self, topic, payload, qos=0, retain=False):
        self.topic = topic
        self.payload = payload
        self.qos = qos
        self.retain = retain


def test_self_heal_auditor_emit():
    recorder = DummyRecorder()
    mqtt = DummyMQTT()

    auditor = SelfHealAuditor(
        line_id="L1",
        event_recorder=recorder,
        mqtt_client_getter=lambda: mqtt,
        topic="events/system/self_heal",
    )

    outcome = SelfHealOutcome(
        timestamp=1700000000,
        decision=SelfHealDecision(action="restart", reason="threshold_reached", details={"failures": 3}),
        executed=True,
        success=True,
        execution=SelfHealExecutionResult(success=True),
        state_updates={"restart_count_hour": 1},
    )

    auditor.emit("edge-api", outcome)

    # 检查 Influx 审计
    assert len(recorder.calls) == 1
    _, tags, fields = recorder.calls[0]
    assert tags["service"] == "edge-api"
    assert fields["success"] == 1

    # 检查 MQTT 发布
    assert mqtt.topic == "events/system/self_heal"
    payload = json.loads(mqtt.payload)
    assert payload["service"] == "edge-api"
    assert payload["state_updates"]["restart_count_hour"] == 1

