"""
控制闭环测试（service 层）

目标：验证 edge-api 控制服务的
- payload 组装（id/ts/tag/value/sig）
- QoS1 发布
- ACK 等待与超时
"""

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
    # 避免依赖真实 tags.yaml：直接注入测试专用可控 tag
    svc._tag_index = {"CTRL_TEST_SPEED": {"control_enabled": True, "range": [0, 50]}}

    resp = await svc.send_set(line="L1", tag="CTRL_TEST_SPEED", value=10, wait_ack=True, timeout_sec=1.0)
    assert resp["success"] is True
    assert resp["status"] == "acked"

    assert published["qos"] == 1
    assert published["topic"] == "control/L1/tags"

    payload = published["payload"]
    assert payload["tag"] == "CTRL_TEST_SPEED"
    assert payload["value"] == 10
    assert "id" in payload and "ts" in payload and "sig" in payload
    assert payload["sig"] == _hmac_sig("secret-1", payload["id"], payload["ts"], payload["tag"], payload["value"])


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
    svc._tag_index = {"CTRL_TEST_SPEED": {"control_enabled": True, "range": [0, 50]}}

    resp = await svc.send_set(line="L1", tag="CTRL_TEST_SPEED", value=10, wait_ack=True, timeout_sec=0.2)
    assert resp["success"] is False
    assert resp["error"] == "ack_timeout"

