"""
控制闭环测试（service 层）

目标：验证 edge-api 控制服务的
- payload 组装（id/ts/tag/value/sig）
- QoS1 发布
- ACK 等待与超时
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]
for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)


async def _noop_write_control_history(*_args, **_kwargs):
    return None


def test_control_command_service_accepts_allowed_control_softpoints():
    from app.services.control_command import ControlCommandService

    svc = ControlCommandService()

    assert svc._validate_tag("VFM1_FREQ_SP", 20.0) == (True, "ok")
    assert svc._validate_tag("VFM1_FREQ_SP", 60.0) == (False, "value_out_of_range")
    assert svc._validate_tag("VFM1_FREQ_SP", 20.5) == (False, "value_not_integer")
    assert svc._validate_tag("VFM1_FREQ_SP", "20") == (False, "value_not_numeric")
    assert svc._validate_tag("VFM1_FREQ_SP", True) == (False, "value_not_numeric")
    assert svc._validate_tag("VF1_FREQ_SP", 20.0) == (False, "softpoint_not_allowed")
    assert svc._validate_tag("M143_RUN", 1.0) == (True, "ok")
    assert svc._validate_tag("M143_RUN", 2.0) == (False, "value_out_of_range")
    assert svc._validate_tag("M1", 1.0) == (False, "softpoint_not_allowed")


@pytest.mark.asyncio
async def test_control_send_set_waits_ack_and_validates_signature(monkeypatch):
    from app.services.control_command import ControlCommandService, _hmac_sig

    monkeypatch.setenv("CONTROL_SIGNATURE_SECRET", "secret-1")
    monkeypatch.setattr(
        "app.services.control_command.write_control_history",
        _noop_write_control_history,
    )

    # fake mqtt manager
    published = {}
    callbacks = {}

    class DummyMQTT:
        connected = True

        def subscribe(self, topic: str, qos: int = 1):
            return None

        def register_callback(self, pattern: str, cb):
            callbacks[pattern] = cb
            return cb

        def publish(self, topic: str, payload: dict, qos: int = 1):
            published["topic"] = topic
            published["payload"] = payload
            published["qos"] = qos
            # 立刻回 ACK（模拟 edge-daq）
            ack_topic = "control/L1/acks"
            ack_payload = json.dumps(
                {"id": payload["id"], "tag": payload["tag"], "status": "success", "value": payload["value"], "ts": 1}
            )
            # service 注册的是 control/+/acks
            cb = callbacks.get("control/+/acks")
            assert cb is not None
            cb(ack_topic, ack_payload)

    monkeypatch.setattr("app.services.control_command.mqtt_manager", DummyMQTT())

    svc = ControlCommandService()

    resp = await svc.send_set(line="L1", tag="VFM1_FREQ_SP", value=10, wait_ack=True, timeout_sec=1.0)
    assert resp["success"] is True
    assert resp["status"] == "acked"

    assert published["qos"] == 1
    assert published["topic"] == "control/L1/tags"

    payload = published["payload"]
    assert payload["tag"] == "VFM1_FREQ_SP"
    assert payload["channel"] == 1
    assert payload["value"] == 10
    assert payload["type"] == "single_frequency_set"
    assert payload["mode"] == 2
    assert "id" in payload and "ts" in payload and "sig" in payload
    assert payload["sig"] == _hmac_sig(
        "secret-1",
        payload["id"],
        payload["ts"],
        payload["tag"],
        payload["value"],
        command_type="single_frequency_set",
        mode=2,
        channel=1,
    )


@pytest.mark.asyncio
async def test_control_send_set_publishes_remote_run_softpoint_without_frequency_fields(monkeypatch):
    from app.services.control_command import ControlCommandService, _hmac_sig

    monkeypatch.setenv("CONTROL_SIGNATURE_SECRET", "secret-1")
    monkeypatch.setattr(
        "app.services.control_command.write_control_history",
        _noop_write_control_history,
    )

    published = {}

    class DummyMQTT:
        connected = True

        def subscribe(self, topic: str, qos: int = 1):
            return None

        def register_callback(self, pattern: str, cb):
            return cb

        def publish(self, topic: str, payload: dict, qos: int = 1):
            published["topic"] = topic
            published["payload"] = payload
            published["qos"] = qos

    monkeypatch.setattr("app.services.control_command.mqtt_manager", DummyMQTT())

    svc = ControlCommandService()

    resp = await svc.send_set(line="L1", tag="M143_RUN", value=1, wait_ack=False, timeout_sec=1.0)
    assert resp["success"] is True
    assert resp["status"] == "accepted"

    payload = published["payload"]
    assert published["topic"] == "control/L1/tags"
    assert payload["tag"] == "M143_RUN"
    assert payload["value"] == 1
    assert "type" not in payload
    assert "mode" not in payload
    assert "channel" not in payload
    assert payload["sig"] == _hmac_sig("secret-1", payload["id"], payload["ts"], "M143_RUN", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_mode", ["AUTO_PAUSED", 1.9, "1.9", True])
async def test_control_send_set_rejects_invalid_mode_before_publish(monkeypatch, bad_mode):
    from app.services.control_command import ControlCommandService

    monkeypatch.setattr(
        "app.services.control_command.write_control_history",
        _noop_write_control_history,
    )

    class DummyMQTT:
        connected = True

        def publish(self, topic: str, payload: dict, qos: int = 1):
            raise AssertionError("invalid mode must not publish")

    monkeypatch.setattr("app.services.control_command.mqtt_manager", DummyMQTT())

    svc = ControlCommandService()

    resp = await svc.send_set(
        line="L1",
        tag="VFM1_FREQ_SP",
        value=10,
        mode=bad_mode,
        wait_ack=False,
    )

    assert resp["success"] is False
    assert resp["error"] == "invalid_mode"


@pytest.mark.asyncio
async def test_control_send_set_rejects_string_value_before_publish(monkeypatch):
    from app.services.control_command import ControlCommandService

    monkeypatch.setattr(
        "app.services.control_command.write_control_history",
        _noop_write_control_history,
    )

    class DummyMQTT:
        connected = True

        def publish(self, topic: str, payload: dict, qos: int = 1):
            raise AssertionError("string value must not publish")

    monkeypatch.setattr("app.services.control_command.mqtt_manager", DummyMQTT())

    svc = ControlCommandService()

    resp = await svc.send_set(
        line="L1",
        tag="VFM1_FREQ_SP",
        value="10",
        mode=2,
        wait_ack=False,
    )

    assert resp["success"] is False
    assert resp["error"] == "value_not_numeric"


@pytest.mark.asyncio
async def test_control_send_set_timeout(monkeypatch):
    from app.services.control_command import ControlCommandService

    monkeypatch.setenv("CONTROL_SIGNATURE_SECRET", "secret-1")
    monkeypatch.setattr(
        "app.services.control_command.write_control_history",
        _noop_write_control_history,
    )

    class DummyMQTT:
        connected = True

        def subscribe(self, topic: str, qos: int = 1):
            return None

        def register_callback(self, pattern: str, cb):
            return cb

        def publish(self, topic: str, payload: dict, qos: int = 1):
            return None

    monkeypatch.setattr("app.services.control_command.mqtt_manager", DummyMQTT())

    svc = ControlCommandService()

    resp = await svc.send_set(line="L1", tag="VFM1_FREQ_SP", value=10, wait_ack=True, timeout_sec=0.2)
    assert resp["success"] is False
    assert resp["error"] == "ack_timeout"


@pytest.mark.asyncio
async def test_control_send_set_returns_failure_on_nak(monkeypatch):
    from app.services.control_command import ControlCommandService

    monkeypatch.setenv("CONTROL_SIGNATURE_SECRET", "secret-1")
    monkeypatch.setattr(
        "app.services.control_command.write_control_history",
        _noop_write_control_history,
    )

    callbacks = {}

    class DummyMQTT:
        connected = True

        def subscribe(self, topic: str, qos: int = 1):
            return None

        def register_callback(self, pattern: str, cb):
            callbacks[pattern] = cb
            return cb

        def publish(self, topic: str, payload: dict, qos: int = 1):
            ack_payload = json.dumps(
                {
                    "id": payload["id"],
                    "tag": payload["tag"],
                    "status": "nak",
                    "reason": "value_out_of_range",
                    "value": payload["value"],
                    "ts": 1,
                }
            )
            callbacks["control/+/acks"]("control/L1/acks", ack_payload)

    monkeypatch.setattr("app.services.control_command.mqtt_manager", DummyMQTT())

    svc = ControlCommandService()

    resp = await svc.send_set(line="L1", tag="VFM1_FREQ_SP", value=10, wait_ack=True, timeout_sec=1.0)

    assert resp["success"] is False
    assert resp["status"] == "nak"
    assert resp["error"] == "control_nak"
    assert resp["data"]["ack_status"] == "nak"
    assert resp["data"]["reason"] == "value_out_of_range"


@pytest.mark.asyncio
async def test_control_send_set_ignores_duplicate_ack(monkeypatch):
    from app.services.control_command import ControlCommandService

    monkeypatch.setenv("CONTROL_SIGNATURE_SECRET", "secret-1")
    monkeypatch.setattr(
        "app.services.control_command.write_control_history",
        _noop_write_control_history,
    )

    callbacks = {}

    class DummyMQTT:
        connected = True

        def subscribe(self, topic: str, qos: int = 1):
            return None

        def register_callback(self, pattern: str, cb):
            callbacks[pattern] = cb
            return cb

        def publish(self, topic: str, payload: dict, qos: int = 1):
            ack_payload = json.dumps(
                {"id": payload["id"], "tag": payload["tag"], "status": "success", "value": payload["value"], "ts": 1}
            )
            cb = callbacks["control/+/acks"]
            cb("control/L1/acks", ack_payload)
            cb("control/L1/acks", ack_payload)

    monkeypatch.setattr("app.services.control_command.mqtt_manager", DummyMQTT())

    loop = asyncio.get_running_loop()
    errors = []
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    try:
        svc = ControlCommandService()

        resp = await svc.send_set(line="L1", tag="VFM1_FREQ_SP", value=10, wait_ack=True, timeout_sec=1.0)
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(old_handler)

    assert resp["success"] is True
    assert errors == []
    assert svc._pending == {}


@pytest.mark.asyncio
async def test_ack_callback_mutates_pending_on_event_loop(monkeypatch):
    from app.services.control_command import ControlCommandService

    svc = ControlCommandService()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    svc._loop = loop
    svc._pending["ack-1"] = fut

    svc._on_ack_message(
        "control/L1/acks",
        json.dumps({"id": "ack-1", "tag": "VFM1_FREQ_SP", "status": "success", "value": 10, "ts": 1}),
    )

    assert "ack-1" in svc._pending

    await asyncio.sleep(0)

    assert fut.done()
    assert fut.result().id == "ack-1"
    assert svc._pending == {}


@pytest.mark.asyncio
async def test_control_send_set_publish_failure_clears_pending(monkeypatch):
    from app.services.control_command import ControlCommandService

    monkeypatch.setenv("CONTROL_SIGNATURE_SECRET", "secret-1")
    monkeypatch.setattr(
        "app.services.control_command.write_control_history",
        _noop_write_control_history,
    )

    class DummyMQTT:
        connected = True

        def subscribe(self, topic: str, qos: int = 1):
            return None

        def register_callback(self, pattern: str, cb):
            return cb

        def publish(self, topic: str, payload: dict, qos: int = 1):
            raise RuntimeError("broker unavailable")

    monkeypatch.setattr("app.services.control_command.mqtt_manager", DummyMQTT())

    svc = ControlCommandService()

    resp = await svc.send_set(line="L1", tag="VFM1_FREQ_SP", value=10, wait_ack=True, timeout_sec=1.0)

    assert resp["success"] is False
    assert resp["error"] == "control_publish_failed"
    assert svc._pending == {}


@pytest.mark.asyncio
async def test_emergency_stop_unavailable_until_softpoint_contract(monkeypatch):
    from app.services.control_command import ControlCommandService

    svc = ControlCommandService()
    send_batch_called = False

    async def fail_if_send_batch_called(*_args, **_kwargs):
        nonlocal send_batch_called
        send_batch_called = True
        return {"success": True}

    monkeypatch.setattr(svc, "send_batch", fail_if_send_batch_called)

    resp = await svc.emergency_stop(line="L1", wait_ack=True, timeout_sec=0.1)

    assert resp["success"] is False
    assert resp["status"] == "unavailable"
    assert resp["error"] == "software_estop_unavailable_pending_softpoint_contract"
    assert resp["data"]["reason"] == "software_estop_softpoint_contract_pending"
    assert send_batch_called is False
