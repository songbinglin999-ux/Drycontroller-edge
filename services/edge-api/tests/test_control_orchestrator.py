import asyncio
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]
for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

from app.services.control_core.models import (  # noqa: E402
    ACTIVE_TARGET_TAGS,
    CONTROL_BASIS_BLOCK,
    CONTROL_STATE_TAGS,
    REQUIRED_SNAPSHOT_TAGS,
    ControlRecommendation,
    SuggestedAction,
    expires_from,
    recommendation_id,
    utc_now,
)
from app.services.control_orchestrator import ControlOrchestrator  # noqa: E402
from app.services.control_state import AUTO_PAUSED, ControlStateService  # noqa: E402


class DummyRealtimeFull:
    async def get_latest_payload(self, requested_tags, lookback):
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
                "M11_I": 48.0,
                "VFM1_ACTIVE_FREQ_TARGET": 30.0,
                "VFM2_ACTIVE_FREQ_TARGET": 30.0,
                "VFM3_ACTIVE_FREQ_TARGET": 30.0,
                "VF1_ACTIVE_FREQ_TARGET": 35.0,
                "VF2_ACTIVE_FREQ_TARGET": 35.0,
                "VF3_ACTIVE_FREQ_TARGET": 35.0,
            }
        )
        return {
            "success": True,
            "data": [{"tag": tag, "value": values[tag]} for tag in requested_tags if tag in values],
            "missing": [],
            "timestamp": 1,
        }

    async def get_history_payload(self, tag, period, window, aggregation):
        values = [10.0, 12.0, 48.0] if tag == "M11_I" else [10.0, 10.0, 10.0]
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


class DummyRealtimeCounting(DummyRealtimeFull):
    def __init__(self):
        self.latest_calls = 0
        self.history_calls = 0
        self.latest_requested_tags = []

    async def get_latest_payload(self, requested_tags, lookback):
        self.latest_calls += 1
        self.latest_requested_tags.append(list(requested_tags))
        return await super().get_latest_payload(requested_tags, lookback)

    async def get_history_payload(self, tag, period, window, aggregation):
        self.history_calls += 1
        return await super().get_history_payload(tag, period, window, aggregation)


class DummyRealtimeMissing:
    async def get_latest_payload(self, requested_tags, lookback):
        return {"success": True, "data": [], "missing": list(requested_tags), "timestamp": 1}

    async def get_history_payload(self, tag, period, window, aggregation):
        return {"success": True, "data": {"tag": tag, "points": []}, "count": 0, "timestamp": 1}


class DummyRealtimeNoPlcControlState(DummyRealtimeFull):
    async def get_latest_payload(self, requested_tags, lookback):
        payload = await super().get_latest_payload(requested_tags, lookback)
        optional_state_tags = {
            "AUTHORITY_OWNER",
            "AUTHORITY_EPOCH",
            "AUTH_TRANSITION_STATE",
            "HMI_DIRECT_ENABLE",
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
    pass


class DummyRealtimeAuto(DummyRealtimeFull):
    pass


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


class DummyRealtimeSnapshotBreaksAfterAuthorize(DummyRealtimeFull):
    def __init__(self):
        self.latest_calls = 0

    async def get_latest_payload(self, requested_tags, lookback):
        self.latest_calls += 1
        if self.latest_calls >= 2:
            raise TypeError("malformed realtime payload")
        return await super().get_latest_payload(requested_tags, lookback)


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


class DummyRaisingCommand(DummyCommand):
    async def send_set(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("mqtt task crashed")


def _actionable_auto_recommendation(snapshot, value: float = 28.0):
    line_id = snapshot.line_id
    now = utc_now()
    return [
        ControlRecommendation(
            id=recommendation_id(line_id, "test_auto_action", "vfm3"),
            line_id=line_id,
            level="warning",
            diagnosis_code="test_auto_action",
            title="Test AUTO action",
            reason="Synthetic actionable recommendation for state-machine tests.",
            evidence=[{"tag": "M11_I", "value": 48.0, "signal": "current"}],
            suggested_actions=[
                SuggestedAction(
                    target_softpoint="VFM3_FREQ_SP",
                    action_type="setpoint_delta",
                    current_value=30.0,
                    suggested_value=value,
                    delta=value - 30.0,
                    unit="Hz",
                    risk_note="Test-only action.",
                )
            ],
            actionable=True,
            created_at=now,
            expires_at=expires_from(now),
        )
    ]


@pytest.mark.asyncio
async def test_orchestrator_recommendations_are_advisory_only(monkeypatch):
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
    recommendation = next(
        item for item in recommendations
        if any(action.get("target_softpoint") for action in item["suggested_actions"])
    )

    assert recommendation["requires_confirmation"] is False
    assert recommendation["actionable"] is False
    assert CONTROL_BASIS_BLOCK in recommendation["blocked_by"]
    assert recommendation["suggested_actions"][0]["target_softpoint"].endswith("_FREQ_SP")
    assert not recommendation["suggested_actions"][0]["target_softpoint"].startswith("M")
    assert "DATA_MISSING_OR_STALE" not in recommendation["blocked_by"]
    assert payload["data"]["recommendation_confirmation_scope"] == "advisory_only"
    assert command.calls == []
    assert {event["action"] for event in audit_events} == {"control.recommendation.generated"}


@pytest.mark.asyncio
async def test_orchestrator_real_tag_snapshot_is_not_stale_without_legacy_tags(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_orchestrator.write_audit", fake_write_audit)
    command = DummyCommand()
    orchestrator = ControlOrchestrator(
        realtime_service=DummyRealtimeFull(),
        command_service=command,
    )

    payload = await orchestrator.get_recommendations(line="L1", lookback="5m")

    assert payload["data"]["freshness"]["status"] == "fresh"
    assert payload["data"]["freshness"]["missing_tags"] == []
    assert all(
        "DATA_MISSING_OR_STALE" not in item["blocked_by"]
        for item in payload["data"]["recommendations"]
    )
    assert command.calls == []


@pytest.mark.asyncio
async def test_orchestrator_marks_plc_local_owner_not_actionable(monkeypatch):
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

    assert recommendation["actionable"] is False
    assert "AUTHORITY_OWNER_NOT_EDGE" in recommendation["blocked_by"]
    assert command.calls == []


@pytest.mark.asyncio
async def test_orchestrator_marks_missing_data_not_actionable(monkeypatch):
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
    assert "DATA_MISSING_OR_STALE" in recommendation["blocked_by"]
    assert command.calls == []
    assert audit_events[-1]["result"] == "success"


@pytest.mark.asyncio
async def test_orchestrator_sends_manual_softpoint_through_gate(monkeypatch):
    audit_events = []

    async def fake_write_audit(**kwargs):
        audit_events.append(kwargs)

    monkeypatch.setattr("app.services.control_orchestrator.write_audit", fake_write_audit)
    command = DummyCommand()
    orchestrator = ControlOrchestrator(
        realtime_service=DummyRealtimeManual(),
        command_service=command,
    )

    sent = await orchestrator.send_manual_setpoint(
        line="L1",
        target_softpoint="VFM1_FREQ_SP",
        value=20.0,
        actor="operator:test",
        operator_id="operator-a",
        request_id="manual-1",
        reason="hmi_panel_submit",
        timeout_sec=1.0,
    )

    assert sent["success"] is True
    assert sent["status"] == "sent"
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
    assert audit_events[-1]["action"] == "control.manual.setpoint"
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

    sent = await orchestrator.send_manual_setpoint(
        line="L1",
        target_softpoint="VF1_FREQ_SP",
        value=25.0,
        actor="operator:test",
        operator_id="operator-a",
        request_id="manual-no-plc-state",
        reason="v1_frequency_target_write",
        timeout_sec=1.0,
    )

    assert sent["success"] is False
    assert sent["error"] == "manual_command_not_actionable"
    assert "DATA_MISSING_OR_STALE" in sent["blocked_by"]
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

    sent = await orchestrator.send_manual_setpoint(
        line="L1",
        target_softpoint="VF1_FREQ_SP",
        value=25.0,
        actor="operator:test",
        operator_id="operator-a",
        request_id="manual-no-transition-state",
        reason="v1_frequency_target_write",
        timeout_sec=1.0,
    )

    assert sent["success"] is False
    assert sent["error"] == "manual_command_not_actionable"
    assert "DATA_MISSING_OR_STALE" in sent["blocked_by"]
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

    sent = await orchestrator.send_manual_setpoint(
        line="L1",
        target_softpoint="VFM1_FREQ_SP",
        value=20.0,
        actor="operator:test",
        operator_id="operator-a",
        request_id="manual-1",
        auto_reauthorization_required=True,
    )

    assert sent["success"] is True
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
        item for item in sent["manual_command"]["evidence"]
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

    sent = await orchestrator.send_manual_setpoint(
        line="L1",
        target_softpoint="M1",
        value=1.0,
        actor="operator:test",
        operator_id="operator-a",
        request_id="manual-motor",
    )

    assert sent["success"] is False
    assert sent["error"] == "manual_command_not_actionable"
    assert "MOTOR_FEEDBACK_WRITE_FORBIDDEN" in sent["blocked_by"]
    assert command.calls == []


@pytest.mark.asyncio
async def test_control_state_authorized_auto_no_action_without_signed_control_basis(monkeypatch):
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
    assert dispatched["status"] == "no_action"
    assert command.calls == []
    assert {event["action"] for event in audit_events} >= {
        "control.mode.auto_authorize",
    }


@pytest.mark.asyncio
async def test_control_status_fast_path_does_not_query_realtime(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_state.write_audit", fake_write_audit)
    realtime = DummyRealtimeCounting()
    service = ControlStateService(
        realtime_service=realtime,
        command_service=DummyCommand(),
        cooldown_sec=0,
        max_delta_hz=10,
    )

    status = await service.get_status(line="L1")

    assert status["mode"] == "MANUAL"
    assert realtime.latest_calls == 0
    assert realtime.history_calls == 0


@pytest.mark.asyncio
async def test_control_auto_authorize_uses_gate_snapshot_without_histories(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_state.write_audit", fake_write_audit)
    realtime = DummyRealtimeCounting()
    service = ControlStateService(
        realtime_service=realtime,
        command_service=DummyCommand(),
        cooldown_sec=0,
        max_delta_hz=10,
    )

    authorized = await service.authorize_auto(line="L1", actor="operator:test")

    assert authorized["success"] is True
    assert realtime.latest_calls == 1
    assert realtime.latest_requested_tags == [list(CONTROL_STATE_TAGS)]
    assert realtime.history_calls == 0


@pytest.mark.asyncio
async def test_control_status_background_gate_refresh_pauses_auto(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_state.write_audit", fake_write_audit)
    service = ControlStateService(
        realtime_service=DummyRealtimeOwnerChanges(),
        command_service=DummyCommand(),
        cooldown_sec=0,
        max_delta_hz=10,
    )

    assert (await service.authorize_auto(line="L1", actor="operator:test"))["success"] is True
    status = await service.get_status(line="L1")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert status["mode"] == "AUTO"
    assert (await service.get_status(line="L1"))["mode"] == AUTO_PAUSED


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
    sent = await orchestrator.send_manual_setpoint(
        line="L1",
        target_softpoint="VFM1_FREQ_SP",
        value=20.0,
        actor="operator:test",
        operator_id="operator-a",
        request_id="manual-1",
    )
    await service.record_command_result(result=sent, command_scope="manual")
    status = await service.get_status(line="L1")

    assert sent["success"] is True
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
async def test_control_state_snapshot_exception_pauses_auto_fail_closed(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_state.write_audit", fake_write_audit)
    command = DummyCommand()
    service = ControlStateService(
        realtime_service=DummyRealtimeSnapshotBreaksAfterAuthorize(),
        command_service=command,
        cooldown_sec=0,
        max_delta_hz=10,
    )

    assert (await service.authorize_auto(line="L1", actor="operator:test"))["success"] is True
    failed = await service.dispatch_auto(line="L1", actor="edge-api:auto", timeout_sec=1.0)

    assert failed["success"] is False
    assert failed["error"] == "auto_gate_failed"
    assert "DATA_MISSING_OR_STALE" in failed["blocked_by"]
    assert "SNAPSHOT_ERROR:TypeError" in failed["blocked_by"]
    assert failed["data"]["mode"] == AUTO_PAUSED
    assert failed["data"]["requires_reauthorization"] is True
    assert command.calls == []


@pytest.mark.asyncio
async def test_control_state_dispatch_exception_pauses_auto_fail_closed(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_state.write_audit", fake_write_audit)
    monkeypatch.setattr("app.services.control_state.build_recommendations", _actionable_auto_recommendation)
    command = DummyRaisingCommand()
    service = ControlStateService(
        realtime_service=DummyRealtimeFull(),
        command_service=command,
        cooldown_sec=0,
        max_delta_hz=10,
    )

    assert (await service.authorize_auto(line="L1", actor="operator:test"))["success"] is True
    failed = await service.dispatch_auto(line="L1", actor="edge-api:auto", timeout_sec=1.0)

    assert failed["success"] is False
    assert failed["error"] == "auto_dispatch_exception"
    assert "AUTO_DISPATCH_EXCEPTION:RuntimeError" in failed["blocked_by"]
    assert failed["data"]["mode"] == AUTO_PAUSED
    assert failed["data"]["requires_reauthorization"] is True
    assert command.calls


@pytest.mark.asyncio
async def test_control_state_nak_pauses_auto_and_records_command(monkeypatch):
    async def fake_write_audit(**kwargs):
        return None

    monkeypatch.setattr("app.services.control_state.write_audit", fake_write_audit)
    monkeypatch.setattr("app.services.control_state.build_recommendations", _actionable_auto_recommendation)
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
    monkeypatch.setattr("app.services.control_state.build_recommendations", _actionable_auto_recommendation)
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
    monkeypatch.setattr("app.services.control_state.build_recommendations", _actionable_auto_recommendation)
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
