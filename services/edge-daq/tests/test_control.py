import json
import time
import hmac
import hashlib
import asyncio

import pytest

from daq.control import (
    ControlSubscriber,
    CONTROL_SIG_INVALID,
    CONTROL_REPLAY_DROPPED,
)


class FakeModbus:
    def __init__(self):
        self.writes = 0
        self.calls = []

    async def write_register(self, address, value, retries=2):
        self.writes += 1
        self.calls.append(("int", address, value, "be"))
        return True

    async def write_float(self, address, value, byte_order="big", retries=2):
        self.writes += 1
        self.calls.append(("float", address, value, byte_order))
        return True

    async def write_int32(self, address, value, byte_order="big", retries=2):
        self.writes += 1
        self.calls.append(("int32", address, value, byte_order))
        return True


class FakeMQTTMsg:
    def __init__(self, payload: bytes):
        self.payload = payload


class FakeMQTTClient:
    def __init__(self, rc=0):
        self.rc = rc
        self.published = []

    def publish(self, topic, payload, qos):
        self.published.append((topic, json.loads(payload), qos))

        class R:
            def __init__(self, rc):
                self.rc = rc

        return R(self.rc)


def _make_sig(secret: str, msg_id: str, ts: float, tag: str, value: float) -> str:
    base = f"{msg_id}:{ts}:{tag}:{value}"
    return hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()


def _mk_sub(monkeypatch, require_sig: bool, secret: str | None):
    monkeypatch.setenv("CONTROL_REQUIRE_SIGNATURE", "true" if require_sig else "false")
    monkeypatch.setenv("CONTROL_REQUIRE_ID", "false")
    monkeypatch.setenv("CONTROL_REQUIRE_TIMESTAMP", "false")
    if secret:
        monkeypatch.setenv("CONTROL_SIGNATURE_SECRET", secret)
    cfg = {
        "tags": {"s": {"c": [{"name": "T", "address": 1, "type": "int", "control_enabled": True}]}},
    }
    modbus = FakeModbus()
    cs = ControlSubscriber(modbus, cfg, line_id="L1")
    cs.client = FakeMQTTClient()
    return cs, modbus


@pytest.mark.asyncio
async def test_control_signature_valid(monkeypatch):
    cs, modbus = _mk_sub(monkeypatch, require_sig=True, secret="secret")
    msg_id = "abc"
    ts = time.time()
    sig = _make_sig("secret", msg_id, ts, "T", 10)
    payload = {"tag": "T", "value": 10, "id": msg_id, "ts": ts, "sig": sig}
    msg = FakeMQTTMsg(json.dumps(payload).encode())

    cs._on_message(None, None, msg)
    await asyncio.sleep(0)

    assert modbus.writes == 1
    assert CONTROL_SIG_INVALID._value.get() == 0


@pytest.mark.asyncio
async def test_control_signature_invalid(monkeypatch):
    before = CONTROL_SIG_INVALID._value.get()
    cs, modbus = _mk_sub(monkeypatch, require_sig=True, secret="secret")
    payload = {"tag": "T", "value": 10, "id": "abc", "ts": time.time(), "sig": "bad"}
    msg = FakeMQTTMsg(json.dumps(payload).encode())

    cs._on_message(None, None, msg)
    await asyncio.sleep(0)

    assert modbus.writes == 0
    assert CONTROL_SIG_INVALID._value.get() == before + 1


@pytest.mark.asyncio
async def test_control_replay_dropped(monkeypatch):
    before = CONTROL_REPLAY_DROPPED._value.get()
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None)
    payload = {"tag": "T", "value": 10, "id": "same", "ts": time.time()}
    msg = FakeMQTTMsg(json.dumps(payload).encode())

    cs._on_message(None, None, msg)
    await asyncio.sleep(0)
    cs._on_message(None, None, msg)
    await asyncio.sleep(0)

    assert modbus.writes == 1
    assert CONTROL_REPLAY_DROPPED._value.get() == before + 1


def test_control_ack_publishes(monkeypatch):
    cs, _ = _mk_sub(monkeypatch, require_sig=False, secret=None)
    msg_id = "abc"
    cs._ack(msg_id, "T", "success", 10)

    assert len(cs.client.published) == 1
    topic, payload, qos = cs.client.published[0]
    assert topic == cs.ack_topic
    assert payload["id"] == msg_id
    assert payload["status"] == "success"
    assert qos == 1
class FakeMsg:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")


@pytest.mark.asyncio
async def test_control_subscriber_scaling_and_rate_limit(monkeypatch):
    modbus = FakeModbus()
    monkeypatch.setenv("CONTROL_REQUIRE_ID", "false")
    monkeypatch.setenv("CONTROL_REQUIRE_TIMESTAMP", "false")
    monkeypatch.setenv("CONTROL_REQUIRE_SIGNATURE", "false")
    config = {
        "tags": {
            "sec": {
                "cat": [
                    {
                        "name": "TAG1",
                        "address": 40010,
                        "type": "float",
                        "scale": 0.1,
                        "control_enabled": True,
                    }
                ]
            }
        }
    }

    cs = ControlSubscriber(modbus, config, line_id="L1")
    cs.min_interval_ms = 200  # 加快测试

    # 不启动真实 MQTT，直接调用 _on_message
    msg = FakeMsg({"tag": "TAG1", "value": 50, "id": "i1", "ts": time.time()})
    cs._on_message(None, None, msg)
    await asyncio.sleep(0)  # 让 create_task 执行

    assert modbus.calls, "应当触发写入"
    call = modbus.calls[0]
    assert call[0] == "float"
    # scale=0.1 -> 寄存器值 = 50 / 0.1 = 500
    assert call[2] == 500

    # 立即再次发送，因限流不应追加调用
    msg2 = FakeMsg({"tag": "TAG1", "value": 60, "id": "i2", "ts": time.time()})
    cs._on_message(None, None, msg2)
    await asyncio.sleep(0)
    assert len(modbus.calls) == 1, "限流应阻止第二次写入"

    # 等待超过限流窗口，再次发送应成功
    cs.last_sent_ts["TAG1"] = time.time() - 1
    cs._on_message(None, None, msg2)
    await asyncio.sleep(0)
    assert len(modbus.calls) == 2


def test_control_disabled_or_bad_tag():
    modbus = FakeModbus()
    config = {
        "tags": {
            "sec": {
                "cat": [
                    {
                        "name": "DISABLED",
                        "address": 1,
                        "type": "int",
                        "control_enabled": False,
                    }
                ]
            }
        }
    }
    cs = ControlSubscriber(modbus, config, line_id="L1")

    # 未知 tag
    cs._on_message(None, None, FakeMsg({"tag": "UNKNOWN", "value": 1}))
    assert not modbus.calls

    # 禁用控制
    cs._on_message(None, None, FakeMsg({"tag": "DISABLED", "value": 1}))
    assert not modbus.calls

