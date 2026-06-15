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
from daq.simulator import PLCSimulator


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


class FakeSubscribeClient:
    def __init__(self, subscribe_rc=0):
        self.subscribe_rc = subscribe_rc
        self.subscriptions = []

    def subscribe(self, topic, qos):
        self.subscriptions.append((topic, qos))
        return (self.subscribe_rc, 1)


def _make_sig(secret: str, msg_id: str, ts: float, tag: str, value: float) -> str:
    base = f"{msg_id}:{ts}:{tag}:{value}"
    return hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()


def _mk_sub(
    monkeypatch,
    require_sig: bool,
    secret: str | None,
    *,
    require_id: bool = False,
    require_ts: bool = False,
    config: dict | None = None,
):
    monkeypatch.setenv("CONTROL_REQUIRE_SIGNATURE", "true" if require_sig else "false")
    monkeypatch.setenv("CONTROL_REQUIRE_ID", "true" if require_id else "false")
    monkeypatch.setenv("CONTROL_REQUIRE_TIMESTAMP", "true" if require_ts else "false")
    if secret:
        monkeypatch.setenv("CONTROL_SIGNATURE_SECRET", secret)
    else:
        monkeypatch.delenv("CONTROL_SIGNATURE_SECRET", raising=False)
    cfg = config or {
        "tags": {"s": {"c": [{"name": "T", "address": 1, "type": "int", "control_enabled": True}]}},
    }
    modbus = FakeModbus()
    cs = ControlSubscriber(modbus, cfg, line_id="L1")
    cs.client = FakeMQTTClient()
    return cs, modbus


def _last_ack(subscriber: ControlSubscriber) -> dict:
    assert subscriber.client.published
    topic, payload, qos = subscriber.client.published[-1]
    assert topic == subscriber.ack_topic
    assert qos == 1
    return payload


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
    ack = _last_ack(cs)
    assert ack["id"] == "abc"
    assert ack["status"] == "nak"
    assert ack["reason"] == "signature_invalid"


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
    ack = _last_ack(cs)
    assert ack["id"] == "same"
    assert ack["status"] == "nak"
    assert ack["reason"] == "replay_detected"


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
    assert cs.get_status()["ack_path"]["ok"] is True


@pytest.mark.asyncio
async def test_control_subscriber_start_failure_is_not_ready(monkeypatch):
    monkeypatch.setenv("MQTT_TLS", "false")
    monkeypatch.setenv("MQTT_ALLOW_INSECURE", "false")
    monkeypatch.setenv("CONTROL_ALLOW_ANONYMOUS", "true")

    config = {
        "tags": {"s": {"c": [{"name": "T", "address": 1, "type": "int", "control_enabled": True}]}},
    }
    subscriber = ControlSubscriber(FakeModbus(), config, line_id="L1")

    await subscriber.start()

    status = subscriber.get_status()
    assert status["ready"] is False
    assert status["reason"] == "start_failed"
    assert status["subscribe_state"] == "start_failed"
    assert "MQTT TLS is required" in status["start_error"]


def test_control_subscribe_disconnect_and_recover_status(monkeypatch):
    subscriber, _ = _mk_sub(monkeypatch, require_sig=False, secret=None)
    subscriber.started = True

    failed_client = FakeSubscribeClient(subscribe_rc=4)
    subscriber._on_connect(failed_client, None, None, 0)
    failed_status = subscriber.get_status()
    assert failed_status["ready"] is False
    assert failed_status["reason"] == "subscribe_failed"
    assert failed_status["mqtt_connected"] is True
    assert failed_status["subscribed"] is False

    recovered_client = FakeSubscribeClient(subscribe_rc=0)
    subscriber._on_connect(recovered_client, None, None, 0)
    pending_status = subscriber.get_status()
    assert pending_status["ready"] is False
    assert pending_status["reason"] == "subscribe_pending"
    assert recovered_client.subscriptions == [(subscriber.topic, 1)]

    subscriber._on_subscribe(recovered_client, None, 1, [1])
    ready_status = subscriber.get_status()
    assert ready_status["ready"] is True
    assert ready_status["subscribed"] is True

    subscriber._on_disconnect(recovered_client, None, 1)
    disconnected_status = subscriber.get_status()
    assert disconnected_status["ready"] is False
    assert disconnected_status["reason"] == "mqtt_disconnected"
    assert disconnected_status["subscribed"] is False

    subscriber._on_connect(recovered_client, None, None, 0)
    subscriber._on_subscribe(recovered_client, None, 2, [1])
    assert subscriber.get_status()["ready"] is True


def test_control_ack_path_failure_and_recovery_affect_status(monkeypatch):
    subscriber, _ = _mk_sub(monkeypatch, require_sig=False, secret=None)
    subscriber.started = True
    subscriber.mqtt_connected = True
    subscriber.subscribed = True
    subscriber.subscribe_state = "subscribed"
    subscriber.client = FakeMQTTClient(rc=1)

    subscriber._ack("ack-fail", "T", "success", 10)

    failed_status = subscriber.get_status()
    assert failed_status["ready"] is False
    assert failed_status["reason"] == "ack_publish_failed"
    assert failed_status["ack_path"]["ok"] is False
    assert failed_status["ack_path"]["last_publish_rc"] == 1

    subscriber.client = FakeMQTTClient(rc=0)
    subscriber._ack("ack-ok", "T", "success", 10)

    recovered_status = subscriber.get_status()
    assert recovered_status["ready"] is True
    assert recovered_status["ack_path"]["ok"] is True


def test_control_rejects_missing_timestamp_with_nak(monkeypatch):
    cs, modbus = _mk_sub(
        monkeypatch,
        require_sig=False,
        secret=None,
        require_id=True,
        require_ts=True,
    )

    cs._on_message(None, None, FakeMQTTMsg(json.dumps({"tag": "T", "value": 10, "id": "missing-ts"}).encode()))

    assert modbus.writes == 0
    ack = _last_ack(cs)
    assert ack["id"] == "missing-ts"
    assert ack["status"] == "nak"
    assert ack["reason"] == "missing_ts"


def test_control_rejects_out_of_range_with_nak(monkeypatch):
    config = {
        "tags": {"s": {"c": [{"name": "T", "address": 1, "type": "int", "control_enabled": True, "range": [0, 50]}]}},
    }
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None, config=config)

    cs._on_message(None, None, FakeMQTTMsg(json.dumps({"tag": "T", "value": 60, "id": "oor"}).encode()))

    assert modbus.writes == 0
    ack = _last_ack(cs)
    assert ack["id"] == "oor"
    assert ack["status"] == "nak"
    assert ack["reason"] == "value_out_of_range"
    assert ack["detail"] == {"range": [0, 50]}


def test_control_rejects_acl_with_nak(monkeypatch):
    monkeypatch.setenv("CONTROL_ALLOWED_TAGS", "OTHER")
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None)

    cs._on_message(None, None, FakeMQTTMsg(json.dumps({"tag": "T", "value": 10, "id": "acl"}).encode()))

    assert modbus.writes == 0
    ack = _last_ack(cs)
    assert ack["id"] == "acl"
    assert ack["status"] == "nak"
    assert ack["reason"] == "tag_not_in_acl"


def test_control_rejects_missing_address_with_nak(monkeypatch):
    config = {
        "tags": {"s": {"c": [{"name": "T", "type": "int", "control_enabled": True}]}},
    }
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None, config=config)

    cs._on_message(None, None, FakeMQTTMsg(json.dumps({"tag": "T", "value": 10, "id": "no-address"}).encode()))

    assert modbus.writes == 0
    ack = _last_ack(cs)
    assert ack["id"] == "no-address"
    assert ack["status"] == "nak"
    assert ack["reason"] == "missing_address"


@pytest.mark.asyncio
async def test_control_subscriber_writes_plc_simulator_and_acks(monkeypatch):
    monkeypatch.setenv("CONTROL_REQUIRE_SIGNATURE", "true")
    monkeypatch.setenv("CONTROL_REQUIRE_ID", "true")
    monkeypatch.setenv("CONTROL_REQUIRE_TIMESTAMP", "true")
    monkeypatch.setenv("CONTROL_SIGNATURE_SECRET", "secret")
    config = {
        "tags": {
            "s": {
                "c": [
                    {
                        "name": "SIM_FREQ_SP",
                        "address": 40100,
                        "type": "float",
                        "control_enabled": True,
                        "range": [0, 50],
                    }
                ]
            }
        },
    }
    simulator = PLCSimulator(config)
    subscriber = ControlSubscriber(simulator, config, line_id="L1")
    subscriber.client = FakeMQTTClient()

    msg_id = "sim-ack-1"
    ts = time.time()
    value = 12.5
    payload = {
        "tag": "SIM_FREQ_SP",
        "value": value,
        "id": msg_id,
        "ts": ts,
        "sig": _make_sig("secret", msg_id, ts, "SIM_FREQ_SP", value),
    }
    subscriber._on_message(None, None, FakeMQTTMsg(json.dumps(payload).encode()))
    await asyncio.sleep(0)

    assert simulator.data[40100] == value
    assert subscriber.client.published
    topic, ack, qos = subscriber.client.published[0]
    assert topic == "control/L1/acks"
    assert ack["id"] == msg_id
    assert ack["status"] == "success"
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
    cs.client = FakeMQTTClient()
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
    ack = _last_ack(cs)
    assert ack["id"] == "i2"
    assert ack["status"] == "nak"
    assert ack["reason"] == "rate_limited"

    # 等待超过限流窗口，同一 id 的限速重试应成功，而不是变成 replay。
    cs.last_sent_ts["TAG1"] = time.time() - 1
    cs._on_message(None, None, msg2)
    await asyncio.sleep(0)
    assert len(modbus.calls) == 2


def test_control_disabled_or_bad_tag(monkeypatch):
    modbus = FakeModbus()
    monkeypatch.setenv("CONTROL_REQUIRE_ID", "false")
    monkeypatch.setenv("CONTROL_REQUIRE_TIMESTAMP", "false")
    monkeypatch.setenv("CONTROL_REQUIRE_SIGNATURE", "false")
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
    cs.client = FakeMQTTClient()

    # 未知 tag
    cs._on_message(None, None, FakeMsg({"tag": "UNKNOWN", "value": 1, "id": "unknown"}))
    assert not modbus.calls
    ack = _last_ack(cs)
    assert ack["id"] == "unknown"
    assert ack["status"] == "nak"
    assert ack["reason"] == "tag_not_found"

    # 禁用控制
    cs._on_message(None, None, FakeMsg({"tag": "DISABLED", "value": 1, "id": "disabled"}))
    assert not modbus.calls
    ack = _last_ack(cs)
    assert ack["id"] == "disabled"
    assert ack["status"] == "nak"
    assert ack["reason"] == "control_not_enabled"
