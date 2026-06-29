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
from app.services.control_state import AUTO_PAUSED, ControlStateService  # noqa: E402


class DummyRealtimeFull:
    async def get_latest_payload(self, requested_tags, lookback):
        values = {tag: 0 for tag in REQUIRED_SNAPSHOT_TAGS}
        values.update(
            {
                "CONTROL_MODE": "AUTO",
                "AUTHORITY_OWNER": "EDGE",
                "AUTHORITY_EPOCH": 1,
                "AUTH_TRANSITION_STATE": 0,
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
            "AUTHORITY_EPOCH",
            "AUTH_TRANSITION_STATE",
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


class DummyRealtimeMissingTransitionState(DummyRealtimeFull):
    async def get_latest_payload(self, requested_tags, lookback):
        payload = await super().get_latest_payload(requested_tags, lookback)
        payload["data"] = [
            row for row in payload["data"]
            if row["tag"] != "AUTH_TRANSITION_STATE"
        ]
        payload["missing"] = ["AUTH_TRANSITION_STATE"]
        return payload


class DummyRealtimeManual(DummyRealtimeFull):
    async def get_latest_payload(self, requested_tags, lookback):
        payload = await super().get_latest_payload(requested_tags, lookback)
        for row in payload["data"]:
            if row["tag"] == "CONTROL_MODE":
                row["value"] = "MANUAL"
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
                    row["value"] = "MANUAL"
        return payload


class DummyRealtimeAuto(DummyRealtimeFull):
    async def get_latest_payload(self, requested_tags, lookback):
        payload = await super().get_latest_payload(requested_tags, lookback)
        for row in payload["data"]:
            if row["tag"] == "CONTROL_MODE":
                row["value"] = "AUTO"
        return payload


class DummyRealtimeLocalOwner(DummyRealtimeFull):
    async def get_latest_payload(self, requested_tags, lookback):
        payload = await super().get_latest_payload(requested_tags, lookback)
        for row in payload["data"]:
            if row["tag"] == "CONTROL_MODE":
                row["value"] = "AUTO"
            if row["tag"] == "AUTHORITY_OWNER":
                row["value"] = "LOCAL_SCREEN"
        return payload


class DummyRealtimeOwnerChanges(DummyRealtimeFull):
    def __init__(self):
        self.latest_calls = 0

    async def get_latest_payload(self, requested_tags, lookback):
        self.latest_calls += 1
        payload = await super().get_latest_payload(requested_tags, lookback)
        if self.latest_calls >= 2:
            for row in payload["data"]:
                if row["tag"] == "AUTHORITY_OWNER":
                    row["value"] = "LOCAL_SCREEN"
        return payload


class DummyCommand:
    def __init__(self):
        self.calls = []
        self.mqtt_connected = True
        self.signature_configured = True

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

    def transport_status(self):
        return {
            "ready": self.mqtt_connected and self.signature_configured,
            "mqtt_connected": self.mqtt_connected,
            "signature_configured": self.signature_configured,
        }


class DummyNakCommand(DummyCommand):
    async def send_set(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "success": False,
            "status": "nak",
            "error": "control_nak",
            "data": {"reason": "plc_nak_5"},
        }


class DummyMismatchCommand(DummyCommand):
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
                "detail": {"active_targets": [0, 0, 0, 0, 0, 0]},
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
    assert command.calls[0]["mode"] == 2
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
    assert "CONTROL_MODE_NOT_AUTO" in confirmed["blocked_by"]
    assert command.calls == []


@pytest.mark.asyncio
async def test_orchestrator_stops_auto_d5100_when_plc_manual_owner_is_visible(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_orchestrator.write_audit", fake_write_audit)
    command = DummyCommand()
    orchestrator = ControlOrchestrator(
        realtime_service=DummyRealtimeLocalOwner(),
        command_service=command,
    )

    payload = await orchestrator.get_recommendations(line="L1", lookback="5m")
    recommendation = next(
        item for item in payload["data"]["recommendations"]
        if any(action.get("target_softpoint") for action in item["suggested_actions"])
    )

    confirmed = await orchestrator.confirm_recommendation(
        recommendation_id=recommendation["id"],
        line="L1",
        actor="operator:test",
        timeout_sec=1.0,
    )

    assert confirmed["success"] is False
    assert confirmed["error"] == "recommendation_not_actionable"
    assert "AUTHORITY_OWNER_NOT_EDGE" in confirmed["blocked_by"]
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
        realtime_service=DummyRealtimeManual(),
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
            "mode": 1,
            "wait_ack": True,
            "timeout_sec": 1.0,
        }
    ]
    assert audit_events[-1]["action"] == "control.recommendation.confirmed"
    assert audit_events[-1]["result"] == "success"


@pytest.mark.asyncio
async def test_orchestrator_rejects_manual_softpoint_without_plc_authority_state(monkeypatch):
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

    assert confirmed["success"] is False
    assert confirmed["error"] == "manual_command_not_actionable"
    assert "DATA_MISSING_OR_STALE" in confirmed["blocked_by"]
    assert command.calls == []


@pytest.mark.asyncio
async def test_orchestrator_rejects_manual_softpoint_without_transition_state(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_orchestrator.write_audit", fake_write_audit)
    command = DummyCommand()
    orchestrator = ControlOrchestrator(
        realtime_service=DummyRealtimeMissingTransitionState(),
        command_service=command,
    )

    confirmed = await orchestrator.confirm_manual_action(
        line="L1",
        target_softpoint="VF1_FREQ_SP",
        value=25.0,
        actor="operator:test",
        operator_id="operator-a",
        request_id="manual-no-transition-state",
        reason="v1_frequency_target_write",
        timeout_sec=1.0,
    )

    assert confirmed["success"] is False
    assert confirmed["error"] == "manual_command_not_actionable"
    assert "DATA_MISSING_OR_STALE" in confirmed["blocked_by"]
    assert command.calls == []


@pytest.mark.asyncio
async def test_orchestrator_manual_softpoint_interrupts_auto_with_manual_d5100(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_orchestrator.write_audit", fake_write_audit)
    command = DummyCommand()
    orchestrator = ControlOrchestrator(
        realtime_service=DummyRealtimeAuto(),
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

    assert confirmed["success"] is True
    assert command.calls == [
        {
            "line": "L1",
            "tag": "VFM1_FREQ_SP",
            "value": 20.0,
            "msg_id": "manual-1",
            "mode": 1,
            "wait_ack": True,
            "timeout_sec": 3.0,
        }
    ]
    operator_request = next(
        item for item in confirmed["recommendation"]["evidence"]
        if item.get("signal") == "operator_request"
    )
    assert operator_request["effective_control_mode"] == "MANUAL"
    assert operator_request["auto_reauthorization_required"] is True


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


@pytest.mark.asyncio
async def test_control_state_authorized_auto_dispatches_single_d5100_command(monkeypatch):
    audit_events = []

    async def fake_write_audit(**kwargs):
        audit_events.append(kwargs)

    monkeypatch.setattr("app.services.control_state.write_audit", fake_write_audit)
    command = DummyCommand()
    service = ControlStateService(
        realtime_service=DummyRealtimeFull(),
        command_service=command,
        cooldown_sec=0,
        max_delta_hz=10,
    )

    authorized = await service.authorize_auto(line="L1", actor="operator:test", reason="shift_start")
    dispatched = await service.dispatch_auto(line="L1", actor="edge-api:auto", timeout_sec=1.0)

    assert authorized["success"] is True
    assert dispatched["success"] is True
    assert dispatched["status"] == "dispatched"
    assert command.calls
    assert command.calls[0]["mode"] == 2
    assert command.calls[0]["wait_ack"] is True
    assert command.calls[0]["tag"] in {"VFM3_FREQ_SP", "VF3_FREQ_SP"}
    assert {event["action"] for event in audit_events} >= {
        "control.mode.auto_authorize",
        "control.auto.dispatch",
    }


@pytest.mark.asyncio
async def test_control_state_manual_frequency_intervention_requires_reauthorization(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_state.write_audit", fake_write_audit)
    monkeypatch.setattr("app.services.control_orchestrator.write_audit", fake_write_audit)
    command = DummyCommand()
    service = ControlStateService(
        realtime_service=DummyRealtimeFull(),
        command_service=command,
        cooldown_sec=0,
        max_delta_hz=10,
    )
    orchestrator = ControlOrchestrator(
        realtime_service=DummyRealtimeAuto(),
        command_service=command,
    )

    assert (await service.authorize_auto(line="L1", actor="operator:test"))["success"] is True
    await service.note_manual_intervention(
        line="L1",
        actor="operator:test",
        target_softpoint="VFM1_FREQ_SP",
        value=20.0,
        reason="hmi_panel_submit",
    )
    confirmed = await orchestrator.confirm_manual_action(
        line="L1",
        target_softpoint="VFM1_FREQ_SP",
        value=20.0,
        actor="operator:test",
        operator_id="operator-a",
        request_id="manual-1",
    )
    await service.record_command_result(result=confirmed, command_scope="manual")
    status = await service.get_status(line="L1")

    assert confirmed["success"] is True
    assert command.calls[-1]["mode"] == 1
    assert status["mode"] == "MANUAL"
    assert status["requires_reauthorization"] is True
    assert status["last_command_result"]["scope"] == "manual"


@pytest.mark.asyncio
async def test_control_state_pauses_when_plc_local_screen_takes_owner(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_state.write_audit", fake_write_audit)
    command = DummyCommand()
    service = ControlStateService(
        realtime_service=DummyRealtimeOwnerChanges(),
        command_service=command,
        cooldown_sec=0,
        max_delta_hz=10,
    )

    assert (await service.authorize_auto(line="L1", actor="operator:test"))["success"] is True
    dispatched = await service.dispatch_auto(line="L1", actor="edge-api:auto", timeout_sec=1.0)

    assert dispatched["success"] is False
    assert dispatched["error"] == "auto_gate_failed"
    assert "AUTHORITY_OWNER_NOT_EDGE" in dispatched["blocked_by"]
    assert dispatched["data"]["mode"] == AUTO_PAUSED
    assert dispatched["data"]["requires_reauthorization"] is True
    assert command.calls == []


@pytest.mark.asyncio
async def test_control_state_dependency_failure_and_recovery_do_not_auto_resume(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_state.write_audit", fake_write_audit)
    command = DummyCommand()
    service = ControlStateService(
        realtime_service=DummyRealtimeFull(),
        command_service=command,
        cooldown_sec=0,
        max_delta_hz=10,
    )

    assert (await service.authorize_auto(line="L1", actor="operator:test"))["success"] is True
    command.mqtt_connected = False
    failed = await service.dispatch_auto(line="L1", actor="edge-api:auto", timeout_sec=1.0)
    command.mqtt_connected = True
    recovered_status = await service.get_status(line="L1")
    blocked_dispatch = await service.dispatch_auto(line="L1", actor="edge-api:auto", timeout_sec=1.0)

    assert failed["success"] is False
    assert "MQTT_NOT_CONNECTED" in failed["blocked_by"]
    assert recovered_status["mode"] == AUTO_PAUSED
    assert recovered_status["requires_reauthorization"] is True
    assert blocked_dispatch["error"] == "auto_not_authorized"
    assert command.calls == []


@pytest.mark.asyncio
async def test_control_state_nak_pauses_auto_and_records_command(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_state.write_audit", fake_write_audit)
    command = DummyNakCommand()
    service = ControlStateService(
        realtime_service=DummyRealtimeFull(),
        command_service=command,
        cooldown_sec=0,
        max_delta_hz=10,
    )

    assert (await service.authorize_auto(line="L1", actor="operator:test"))["success"] is True
    dispatched = await service.dispatch_auto(line="L1", actor="edge-api:auto", timeout_sec=1.0)

    assert dispatched["success"] is False
    assert dispatched["error"] == "auto_command_failed"
    assert "control_nak" in dispatched["blocked_by"]
    assert dispatched["data"]["mode"] == AUTO_PAUSED
    assert dispatched["data"]["last_command_result"]["scope"] == "auto"


@pytest.mark.asyncio
async def test_control_state_cooldown_rejects_next_auto_command(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_state.write_audit", fake_write_audit)
    command = DummyCommand()
    service = ControlStateService(
        realtime_service=DummyRealtimeFull(),
        command_service=command,
        cooldown_sec=60,
        max_delta_hz=10,
    )

    assert (await service.authorize_auto(line="L1", actor="operator:test"))["success"] is True
    first = await service.dispatch_auto(line="L1", actor="edge-api:auto", timeout_sec=1.0)
    second = await service.dispatch_auto(line="L1", actor="edge-api:auto", timeout_sec=1.0)

    assert first["success"] is True
    assert second["success"] is False
    assert second["error"] == "auto_rate_gate_failed"
    assert "AUTO_COOLDOWN_ACTIVE" in second["blocked_by"]
    assert second["data"]["mode"] == AUTO_PAUSED
    assert len(command.calls) == 1


@pytest.mark.asyncio
async def test_control_state_active_target_mismatch_pauses_auto(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_state.write_audit", fake_write_audit)
    command = DummyMismatchCommand()
    service = ControlStateService(
        realtime_service=DummyRealtimeFull(),
        command_service=command,
        cooldown_sec=0,
        max_delta_hz=10,
    )

    assert (await service.authorize_auto(line="L1", actor="operator:test"))["success"] is True
    dispatched = await service.dispatch_auto(line="L1", actor="edge-api:auto", timeout_sec=1.0)

    assert dispatched["success"] is False
    assert "active_targets_mismatch" in dispatched["blocked_by"]
    assert dispatched["data"]["mode"] == AUTO_PAUSED
