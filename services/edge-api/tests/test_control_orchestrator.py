import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]
for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

from app.services.control_core.models import REQUIRED_SNAPSHOT_TAGS  # noqa: E402
from app.services.control_orchestrator import ControlOrchestrator  # noqa: E402


class DummyRealtimeFull:
    async def get_latest_payload(self, requested_tags, lookback):
        values = {tag: 0 for tag in REQUIRED_SNAPSHOT_TAGS}
        values.update(
            {
                "CONTROL_MODE": "SEMI_AUTO",
                "AUTHORITY_OWNER": "EDGE",
                "HMI_DIRECT_ENABLE": 0,
                "MAINTENANCE_LOCK": 0,
                "EMERGENCY_STOP": 0,
                "FTT1": 70.0,
                "FTT2": 70.0,
                "FTT3": 70.0,
                "T_OUT2": 65.0,
                "T_OUT4": 66.0,
                "TT1": 82.0,
                "TT2": 80.0,
                "MT1": 13.0,
                "VFM1": 30.0,
                "VFM2": 30.0,
                "VFM3": 30.0,
                "VF1": 35.0,
                "VF2": 35.0,
                "VF3": 35.0,
                "I_M11": 48.0,
            }
        )
        for index in range(1, 15):
            values.setdefault(f"M{index}", 1)
            values.setdefault(f"I_M{index}", 10.0)
        values["M11"] = 1
        return {
            "success": True,
            "data": [{"tag": tag, "value": values[tag]} for tag in requested_tags],
            "missing": [],
            "timestamp": 1,
        }

    async def get_history_payload(self, tag, period, window, aggregation):
        values = [10.0, 12.0, 48.0] if tag == "I_M11" else [10.0, 10.0, 10.0]
        return {
            "success": True,
            "data": {
                "tag": tag,
                "points": [
                    {"time": "2026-06-15T00:00:00Z", "value": values[0]},
                    {"time": "2026-06-15T00:02:30Z", "value": values[1]},
                    {"time": "2026-06-15T00:05:00Z", "value": values[2]},
                ],
            },
            "count": 3,
            "timestamp": 1,
        }


class DummyRealtimeMissing:
    async def get_latest_payload(self, requested_tags, lookback):
        return {"success": True, "data": [], "missing": list(requested_tags), "timestamp": 1}

    async def get_history_payload(self, tag, period, window, aggregation):
        return {"success": True, "data": {"tag": tag, "points": []}, "count": 0, "timestamp": 1}


class DummyRealtimeNoPlcControlState(DummyRealtimeFull):
    async def get_latest_payload(self, requested_tags, lookback):
        payload = await super().get_latest_payload(requested_tags, lookback)
        optional_state_tags = {
            "CONTROL_MODE",
            "AUTHORITY_OWNER",
            "HMI_DIRECT_ENABLE",
            "MAINTENANCE_LOCK",
            "EMERGENCY_STOP",
        }
        payload["data"] = [
            row for row in payload["data"]
            if row["tag"] not in optional_state_tags
        ]
        payload["missing"] = sorted(optional_state_tags)
        return payload


class DummyRealtimeModeChanges(DummyRealtimeFull):
    def __init__(self):
        self.latest_calls = 0

    async def get_latest_payload(self, requested_tags, lookback):
        self.latest_calls += 1
        payload = await super().get_latest_payload(requested_tags, lookback)
        if self.latest_calls >= 2:
            for row in payload["data"]:
                if row["tag"] == "CONTROL_MODE":
                    row["value"] = "MANUAL_ASSIST"
        return payload


class DummyRealtimeAutoPilot(DummyRealtimeFull):
    async def get_latest_payload(self, requested_tags, lookback):
        payload = await super().get_latest_payload(requested_tags, lookback)
        for row in payload["data"]:
            if row["tag"] == "CONTROL_MODE":
                row["value"] = "AUTO_PILOT"
        return payload


class DummyCommand:
    def __init__(self):
        self.calls = []

    async def send_set(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "success": True,
            "status": "acked",
            "data": {
                "id": "ctl_1",
                "tag": kwargs["tag"],
                "ack_status": "success",
                "value": kwargs["value"],
            },
        }


@pytest.mark.asyncio
async def test_orchestrator_confirms_only_actionable_softpoints(monkeypatch):
    audit_events = []

    async def fake_write_audit(**kwargs):
        audit_events.append(kwargs)

    monkeypatch.setattr("app.services.control_orchestrator.write_audit", fake_write_audit)
    command = DummyCommand()
    orchestrator = ControlOrchestrator(
        realtime_service=DummyRealtimeFull(),
        command_service=command,
    )

    payload = await orchestrator.get_recommendations(line="L1", lookback="5m")
    recommendations = payload["data"]["recommendations"]
    recommendation = next(item for item in recommendations if item["actionable"])

    assert recommendation["requires_confirmation"] is True
    assert recommendation["suggested_actions"][0]["target_softpoint"].endswith("_FREQ_SP")
    assert not recommendation["suggested_actions"][0]["target_softpoint"].startswith("M")

    confirmed = await orchestrator.confirm_recommendation(
        recommendation_id=recommendation["id"],
        line="L1",
        actor="operator:test",
        timeout_sec=1.0,
    )

    assert confirmed["success"] is True
    assert command.calls
    assert command.calls[0]["tag"].endswith("_FREQ_SP")
    assert command.calls[0]["tag"] in {"VFM3_FREQ_SP", "VF3_FREQ_SP"}
    assert {event["action"] for event in audit_events} >= {
        "control.recommendation.generated",
        "control.recommendation.confirmed",
    }


@pytest.mark.asyncio
async def test_orchestrator_rechecks_mode_at_confirmation(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_orchestrator.write_audit", fake_write_audit)
    command = DummyCommand()
    orchestrator = ControlOrchestrator(
        realtime_service=DummyRealtimeModeChanges(),
        command_service=command,
    )

    payload = await orchestrator.get_recommendations(line="L1", lookback="5m")
    recommendation = next(item for item in payload["data"]["recommendations"] if item["actionable"])

    confirmed = await orchestrator.confirm_recommendation(
        recommendation_id=recommendation["id"],
        line="L1",
        actor="operator:test",
        timeout_sec=1.0,
    )

    assert confirmed["success"] is False
    assert confirmed["error"] == "recommendation_not_actionable"
    assert "CONTROL_MODE_NOT_SEMI_AUTO" in confirmed["blocked_by"]
    assert command.calls == []


@pytest.mark.asyncio
async def test_orchestrator_blocks_confirmation_when_data_missing(monkeypatch):
    audit_events = []

    async def fake_write_audit(**kwargs):
        audit_events.append(kwargs)

    monkeypatch.setattr("app.services.control_orchestrator.write_audit", fake_write_audit)
    command = DummyCommand()
    orchestrator = ControlOrchestrator(
        realtime_service=DummyRealtimeMissing(),
        command_service=command,
    )

    payload = await orchestrator.get_recommendations(line="L1", lookback="5m")
    recommendation = payload["data"]["recommendations"][0]
    assert recommendation["diagnosis_code"] == "data_degraded"
    assert recommendation["actionable"] is False

    confirmed = await orchestrator.confirm_recommendation(
        recommendation_id=recommendation["id"],
        line="L1",
        actor="operator:test",
        timeout_sec=1.0,
    )

    assert confirmed["success"] is False
    assert confirmed["error"] == "recommendation_not_actionable"
    assert "DATA_MISSING_OR_STALE" in confirmed["blocked_by"]
    assert command.calls == []
    assert audit_events[-1]["result"] == "denied"


@pytest.mark.asyncio
async def test_orchestrator_confirms_manual_softpoint_through_gate(monkeypatch):
    audit_events = []

    async def fake_write_audit(**kwargs):
        audit_events.append(kwargs)

    monkeypatch.setattr("app.services.control_orchestrator.write_audit", fake_write_audit)
    command = DummyCommand()
    orchestrator = ControlOrchestrator(
        realtime_service=DummyRealtimeFull(),
        command_service=command,
    )

    confirmed = await orchestrator.confirm_manual_action(
        line="L1",
        target_softpoint="VFM1_FREQ_SP",
        value=20.0,
        actor="operator:test",
        operator_id="operator-a",
        request_id="manual-1",
        reason="hmi_panel_submit",
        timeout_sec=1.0,
    )

    assert confirmed["success"] is True
    assert command.calls == [
        {
            "line": "L1",
            "tag": "VFM1_FREQ_SP",
            "value": 20.0,
            "msg_id": "manual-1",
            "wait_ack": True,
            "timeout_sec": 1.0,
        }
    ]
    assert audit_events[-1]["action"] == "control.recommendation.confirmed"
    assert audit_events[-1]["result"] == "success"


@pytest.mark.asyncio
async def test_orchestrator_confirms_manual_softpoint_without_plc_control_state(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_orchestrator.write_audit", fake_write_audit)
    command = DummyCommand()
    orchestrator = ControlOrchestrator(
        realtime_service=DummyRealtimeNoPlcControlState(),
        command_service=command,
    )

    confirmed = await orchestrator.confirm_manual_action(
        line="L1",
        target_softpoint="VF1_FREQ_SP",
        value=25.0,
        actor="operator:test",
        operator_id="operator-a",
        request_id="manual-no-plc-state",
        reason="v1_frequency_target_write",
        timeout_sec=1.0,
    )

    assert confirmed["success"] is True
    assert confirmed["recommendation"]["blocked_by"] == []
    assert command.calls == [
        {
            "line": "L1",
            "tag": "VF1_FREQ_SP",
            "value": 25.0,
            "msg_id": "manual-no-plc-state",
            "wait_ack": True,
            "timeout_sec": 1.0,
        }
    ]


@pytest.mark.asyncio
async def test_orchestrator_blocks_manual_softpoint_in_auto_pilot_mode(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_orchestrator.write_audit", fake_write_audit)
    command = DummyCommand()
    orchestrator = ControlOrchestrator(
        realtime_service=DummyRealtimeAutoPilot(),
        command_service=command,
    )

    confirmed = await orchestrator.confirm_manual_action(
        line="L1",
        target_softpoint="VFM1_FREQ_SP",
        value=20.0,
        actor="operator:test",
        operator_id="operator-a",
        request_id="manual-1",
    )

    assert confirmed["success"] is False
    assert confirmed["error"] == "manual_command_not_actionable"
    assert "CONTROL_MODE_NOT_MANUAL_OR_SEMI_AUTO" in confirmed["blocked_by"]
    assert command.calls == []


@pytest.mark.asyncio
async def test_orchestrator_blocks_manual_motor_feedback_write(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_orchestrator.write_audit", fake_write_audit)
    command = DummyCommand()
    orchestrator = ControlOrchestrator(
        realtime_service=DummyRealtimeFull(),
        command_service=command,
    )

    confirmed = await orchestrator.confirm_manual_action(
        line="L1",
        target_softpoint="M1",
        value=1.0,
        actor="operator:test",
        operator_id="operator-a",
        request_id="manual-motor",
    )

    assert confirmed["success"] is False
    assert confirmed["error"] == "manual_command_not_actionable"
    assert "MOTOR_FEEDBACK_WRITE_FORBIDDEN" in confirmed["blocked_by"]
    assert command.calls == []
