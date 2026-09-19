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
    SINGLE_FREQUENCY_COMMAND_TYPE,
    _build_tag_index,
)
from daq.frequency_transaction import FrequencyTransactionResult


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

    async def write_coil(self, address, value, retries=2):
        self.writes += 1
        self.calls.append(("coil", address, value))
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


class FakeFrequencyWriter:
    def __init__(self, result: FrequencyTransactionResult):
        self.result = result
        self.calls = []

    async def write_one(self, channel, value, **kwargs):
        self.calls.append((channel, value, kwargs))
        return self.result


class FakeSubscribeClient:
    def __init__(self, subscribe_rc=0):
        self.subscribe_rc = subscribe_rc
        self.subscriptions = []

    def subscribe(self, topic, qos):
        self.subscriptions.append((topic, qos))
        return (self.subscribe_rc, 1)


CONTROL_TAG = "VFM1_FREQ_SP"


def _address_map_softpoint(address: object, scale: object = 1.0, *, tag: str = CONTROL_TAG):
    return {
        "modbus_address_map": {
            tag: {
                "address": address,
                "function_code": 6,
                "scale": scale,
                "byte_order": "big",
                "writable": True,
                "type": "float",
                "range": [0.0, 50.0],
                "unit": "Hz",
            }
        }
    }


def _address_map_remote_run_softpoint():
    return {
        "modbus_address_map": {
            "EDGE_REMOTE_CONTROL_ENABLE": {
                "address": 31000,
                "function_code": 5,
                "scale": 1,
                "byte_order": "big",
                "writable": True,
                "type": "bool",
                "range": [0, 1],
            },
            "M143_RUN": {
                "address": 30143,
                "function_code": 5,
                "scale": 1,
                "byte_order": "big",
                "writable": True,
                "type": "bool",
                "range": [0, 1],
                "requires_edge_enable": True,
                "enable_address": 31000,
                "enable_function_code": 5,
            },
        }
    }


def _make_sig(
    secret: str,
    msg_id: str,
    ts: float,
    tag: str,
    value: float,
    *,
    command_type: str = "",
    mode: int | None = None,
    channel: int | None = None,
) -> str:
    base = f"{msg_id}:{ts}:{tag}:{value}"
    if command_type == SINGLE_FREQUENCY_COMMAND_TYPE:
        base = f"{base}:{command_type}:{mode}:{channel}"
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
    cfg = config or _address_map_softpoint(address=1)
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


def test_heartbeat_entries_are_not_control_softpoints():
    cfg = _address_map_softpoint(address=40030)
    cfg["modbus_address_map"].update(
        {
            "PLC_HEARTBEAT": {
                "address": 45000,
                "function_code": 3,
                "scale": 1,
                "byte_order": "big",
                "type": "int",
                "diagnostic": "heartbeat",
            },
            "EDGE_HOST_ALIVE": {
                "address": 45001,
                "function_code": 6,
                "scale": 1,
                "byte_order": "big",
                "writable": True,
                "type": "int",
                "diagnostic": "heartbeat",
                "control_softpoint": False,
            },
        }
    )

    index = _build_tag_index(cfg)

    assert set(index) == {CONTROL_TAG}


@pytest.mark.asyncio
async def test_remote_run_softpoint_sets_edge_enable_before_output_coil(monkeypatch):
    cs, modbus = _mk_sub(
        monkeypatch,
        require_sig=False,
        secret=None,
        config=_address_map_remote_run_softpoint(),
    )

    cs._on_message(None, None, FakeMQTTMsg(json.dumps({"tag": "M143_RUN", "value": 1, "id": "run-143"}).encode()))
    await asyncio.sleep(0)

    assert modbus.calls == [("coil", 31000, True), ("coil", 30143, True)]
    ack = _last_ack(cs)
    assert ack["id"] == "run-143"
    assert ack["tag"] == "M143_RUN"
    assert ack["status"] == "success"


def test_single_frequency_transaction_command_is_disabled_by_default(monkeypatch):
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None, config={})

    cs._on_message(
        None,
        None,
        FakeMQTTMsg(
            json.dumps(
                {
                    "type": SINGLE_FREQUENCY_COMMAND_TYPE,
                    "tag": "VFM1_FREQ_SP",
                    "value": 31,
                    "id": "single-disabled",
                }
            ).encode()
        ),
    )

    assert modbus.writes == 0
    ack = _last_ack(cs)
    assert ack["id"] == "single-disabled"
    assert ack["tag"] == "VFM1_FREQ_SP"
    assert ack["status"] == "nak"
    assert ack["reason"] == "d5100_writer_disabled"


@pytest.mark.asyncio
async def test_single_frequency_transaction_command_uses_d5100_writer_when_enabled(monkeypatch):
    monkeypatch.setenv("CONTROL_ENABLE_D5100_WRITER", "true")
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None, config={})
    fake_writer = FakeFrequencyWriter(
        FrequencyTransactionResult(
            status="success",
            seq=21,
            channel=1,
            value=31,
            ack_seq=21,
            active_targets=[31, 0, 0, 0, 0, 0],
        )
    )
    cs.frequency_writer = fake_writer

    cs._on_message(
        None,
        None,
        FakeMQTTMsg(
            json.dumps(
                {
                    "type": SINGLE_FREQUENCY_COMMAND_TYPE,
                    "tag": "VFM1_FREQ_SP",
                    "channel": 1,
                    "value": 31,
                    "mode": 2,
                    "seq": 21,
                    "id": "single-ok",
                }
            ).encode()
        ),
    )
    await asyncio.sleep(0)

    assert modbus.writes == 0
    assert fake_writer.calls == [
        (
            1,
            31,
            {"mode": 2, "seq": 21, "timeout_sec": 3.0, "refresh_heartbeat": False},
        )
    ]
    ack = _last_ack(cs)
    assert ack["id"] == "single-ok"
    assert ack["tag"] == "VFM1_FREQ_SP"
    assert ack["status"] == "success"
    assert ack["detail"]["seq"] == 21
    assert ack["detail"]["channel"] == 1
    assert ack["detail"]["value"] == 31
    assert ack["detail"]["active_targets"] == [31, 0, 0, 0, 0, 0]


@pytest.mark.parametrize(
    "payload_update,reason",
    [
        ({"channel": None}, "missing_channel"),
        ({"channel": 1.9}, "invalid_channel"),
        ({"channel": "1.9"}, "invalid_channel"),
        ({"channel": 2}, "channel_tag_mismatch"),
        ({"mode": None}, "missing_mode"),
        ({"mode": 1.9}, "invalid_mode"),
        ({"mode": "1.9"}, "invalid_mode"),
        ({"mode": 3}, "invalid_mode"),
    ],
)
def test_single_frequency_transaction_rejects_bad_gate_fields(monkeypatch, payload_update, reason):
    monkeypatch.setenv("CONTROL_ENABLE_D5100_WRITER", "true")
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None, config={})
    fake_writer = FakeFrequencyWriter(FrequencyTransactionResult(status="success", seq=1))
    cs.frequency_writer = fake_writer
    payload = {
        "type": SINGLE_FREQUENCY_COMMAND_TYPE,
        "tag": "VFM1_FREQ_SP",
        "channel": 1,
        "value": 31,
        "mode": 2,
        "id": f"bad-{reason}",
    }
    for key, value in payload_update.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value

    cs._on_message(None, None, FakeMQTTMsg(json.dumps(payload).encode()))

    assert modbus.writes == 0
    assert fake_writer.calls == []
    ack = _last_ack(cs)
    assert ack["id"] == f"bad-{reason}"
    assert ack["status"] == "nak"
    assert ack["reason"] == reason


@pytest.mark.asyncio
async def test_single_frequency_transactions_are_queued_one_at_a_time(monkeypatch):
    monkeypatch.setenv("CONTROL_ENABLE_D5100_WRITER", "true")
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None, config={})
    call_order = []

    class SlowFrequencyWriter:
        async def write_one(self, channel, value, **kwargs):
            call_order.append(("start", channel, value))
            await asyncio.sleep(0.02)
            call_order.append(("done", channel, value))
            return FrequencyTransactionResult(
                status="success",
                seq=kwargs["seq"],
                channel=channel,
                value=value,
                ack_seq=kwargs["seq"],
                active_targets=[31 if channel == 1 else 0, 32 if channel == 2 else 0, 0, 0, 0, 0],
            )

    cs.frequency_writer = SlowFrequencyWriter()

    cs._on_message(
        None,
        None,
        FakeMQTTMsg(
            json.dumps(
                {
                    "type": SINGLE_FREQUENCY_COMMAND_TYPE,
                    "tag": "VFM1_FREQ_SP",
                    "channel": 1,
                    "value": 31,
                    "mode": 2,
                    "seq": 31,
                    "id": "queue-1",
                }
            ).encode()
        ),
    )
    cs._on_message(
        None,
        None,
        FakeMQTTMsg(
            json.dumps(
                {
                    "type": SINGLE_FREQUENCY_COMMAND_TYPE,
                    "tag": "VFM2_FREQ_SP",
                    "channel": 2,
                    "value": 32,
                    "mode": 2,
                    "seq": 32,
                    "id": "queue-2",
                }
            ).encode()
        ),
    )
    await asyncio.sleep(0.06)

    assert modbus.writes == 0
    assert call_order == [
        ("start", 1, 31),
        ("done", 1, 31),
        ("start", 2, 32),
        ("done", 2, 32),
    ]
    assert [payload["id"] for _, payload, _ in cs.client.published[-2:]] == ["queue-1", "queue-2"]


@pytest.mark.asyncio
async def test_control_signature_valid(monkeypatch):
    monkeypatch.setenv("CONTROL_ENABLE_D5100_WRITER", "true")
    cs, modbus = _mk_sub(monkeypatch, require_sig=True, secret="secret")
    fake_writer = FakeFrequencyWriter(
        FrequencyTransactionResult(
            status="success",
            seq=1,
            channel=1,
            value=10,
            ack_seq=1,
            active_targets=[10, 0, 0, 0, 0, 0],
        )
    )
    cs.frequency_writer = fake_writer
    msg_id = "abc"
    ts = time.time()
    sig = _make_sig(
        "secret",
        msg_id,
        ts,
        CONTROL_TAG,
        10,
        command_type=SINGLE_FREQUENCY_COMMAND_TYPE,
        mode=2,
        channel=1,
    )
    payload = {
        "type": SINGLE_FREQUENCY_COMMAND_TYPE,
        "tag": CONTROL_TAG,
        "channel": 1,
        "value": 10,
        "mode": 2,
        "id": msg_id,
        "ts": ts,
        "sig": sig,
    }
    msg = FakeMQTTMsg(json.dumps(payload).encode())

    cs._on_message(None, None, msg)
    await asyncio.sleep(0)

    assert modbus.writes == 0
    assert fake_writer.calls
    assert CONTROL_SIG_INVALID._value.get() == 0


@pytest.mark.asyncio
async def test_control_signature_invalid(monkeypatch):
    before = CONTROL_SIG_INVALID._value.get()
    cs, modbus = _mk_sub(monkeypatch, require_sig=True, secret="secret")
    payload = {"tag": CONTROL_TAG, "value": 10, "id": "abc", "ts": time.time(), "sig": "bad"}
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
@pytest.mark.parametrize(
    "poison_reason",
    [
        "missing_tag",
        "missing_ts",
        "invalid_ts",
        "timestamp_out_of_window",
        "signature_secret_missing",
        "signature_missing",
        "signature_invalid",
    ],
)
async def test_pre_auth_rejects_do_not_poison_id_cache(monkeypatch, poison_reason):
    monkeypatch.setenv("CONTROL_ENABLE_D5100_WRITER", "true")
    secret = None if poison_reason == "signature_secret_missing" else "secret"
    cs, modbus = _mk_sub(
        monkeypatch,
        require_sig=True,
        secret=secret,
        require_id=True,
        require_ts=True,
    )
    fake_writer = FakeFrequencyWriter(
        FrequencyTransactionResult(
            status="success",
            seq=1,
            channel=1,
            value=10,
            ack_seq=1,
            active_targets=[10, 0, 0, 0, 0, 0],
        )
    )
    cs.frequency_writer = fake_writer
    msg_id = f"same-id-{poison_reason}"
    value = 10
    valid_ts = time.time()
    valid_payload = {
        "type": SINGLE_FREQUENCY_COMMAND_TYPE,
        "tag": CONTROL_TAG,
        "channel": 1,
        "value": value,
        "mode": 2,
        "id": msg_id,
        "ts": valid_ts,
        "sig": _make_sig(
            "secret",
            msg_id,
            valid_ts,
            CONTROL_TAG,
            value,
            command_type=SINGLE_FREQUENCY_COMMAND_TYPE,
            mode=2,
            channel=1,
        ),
    }

    if poison_reason == "missing_tag":
        bad_payload = {"value": value, "id": msg_id, "ts": valid_ts, "sig": "bad"}
    elif poison_reason == "missing_ts":
        bad_payload = {"tag": CONTROL_TAG, "value": value, "id": msg_id, "sig": "bad"}
    elif poison_reason == "invalid_ts":
        bad_payload = {"tag": CONTROL_TAG, "value": value, "id": msg_id, "ts": "bad", "sig": "bad"}
    elif poison_reason == "timestamp_out_of_window":
        stale_ts = valid_ts - 3600
        bad_payload = {
            "tag": CONTROL_TAG,
            "channel": 1,
            "value": value,
            "mode": 2,
            "id": msg_id,
            "ts": stale_ts,
            "sig": _make_sig(
                "secret",
                msg_id,
                stale_ts,
                CONTROL_TAG,
                value,
                command_type=SINGLE_FREQUENCY_COMMAND_TYPE,
                mode=2,
                channel=1,
            ),
        }
    elif poison_reason == "signature_secret_missing":
        bad_payload = valid_payload
    elif poison_reason == "signature_missing":
        bad_payload = {k: v for k, v in valid_payload.items() if k != "sig"}
    else:
        bad_payload = {**valid_payload, "sig": "bad"}

    cs._on_message(None, None, FakeMQTTMsg(json.dumps(bad_payload).encode()))
    await asyncio.sleep(0)

    assert modbus.writes == 0
    bad_ack = _last_ack(cs)
    assert bad_ack["id"] == msg_id
    assert bad_ack["status"] == "nak"
    assert bad_ack["reason"] == poison_reason
    assert cs._is_duplicate(msg_id) is False

    if poison_reason == "signature_secret_missing":
        cs.signature_secret = "secret"

    cs._on_message(None, None, FakeMQTTMsg(json.dumps(valid_payload).encode()))
    await asyncio.sleep(0)

    assert modbus.writes == 0
    assert fake_writer.calls
    ack = _last_ack(cs)
    assert ack["id"] == msg_id
    assert ack["status"] == "success"


@pytest.mark.asyncio
async def test_control_replay_dropped(monkeypatch):
    before = CONTROL_REPLAY_DROPPED._value.get()
    monkeypatch.setenv("CONTROL_ENABLE_D5100_WRITER", "true")
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None)
    cs.frequency_writer = FakeFrequencyWriter(
        FrequencyTransactionResult(
            status="success",
            seq=1,
            channel=1,
            value=10,
            ack_seq=1,
            active_targets=[10, 0, 0, 0, 0, 0],
        )
    )
    payload = {
        "type": SINGLE_FREQUENCY_COMMAND_TYPE,
        "tag": CONTROL_TAG,
        "channel": 1,
        "value": 10,
        "mode": 2,
        "id": "same",
        "ts": time.time(),
    }
    msg = FakeMQTTMsg(json.dumps(payload).encode())

    cs._on_message(None, None, msg)
    await asyncio.sleep(0)
    cs._on_message(None, None, msg)
    await asyncio.sleep(0)

    assert modbus.writes == 0
    assert CONTROL_REPLAY_DROPPED._value.get() == before + 1
    ack = _last_ack(cs)
    assert ack["id"] == "same"
    assert ack["status"] == "nak"
    assert ack["reason"] == "replay_detected"


def test_control_replay_reservation_is_atomic(monkeypatch):
    monkeypatch.setenv("CONTROL_ENABLE_D5100_WRITER", "true")
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None)
    assert cs._remember_id_if_new("reserved") is True
    assert cs._remember_id_if_new("reserved") is False

    payload = {
        "type": SINGLE_FREQUENCY_COMMAND_TYPE,
        "tag": CONTROL_TAG,
        "channel": 1,
        "value": 10,
        "mode": 2,
        "id": "reserved",
        "ts": time.time(),
    }
    cs._on_message(None, None, FakeMQTTMsg(json.dumps(payload).encode()))

    assert modbus.writes == 0
    ack = _last_ack(cs)
    assert ack["id"] == "reserved"
    assert ack["status"] == "nak"
    assert ack["reason"] == "replay_detected"


def test_control_closed_captured_loop_falls_back_without_crash(monkeypatch):
    monkeypatch.setenv("CONTROL_ENABLE_D5100_WRITER", "true")
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None)
    cs.frequency_writer = FakeFrequencyWriter(
        FrequencyTransactionResult(
            status="success",
            seq=1,
            channel=1,
            value=10,
            ack_seq=1,
            active_targets=[10, 0, 0, 0, 0, 0],
        )
    )
    loop = asyncio.new_event_loop()
    loop.close()
    cs._loop = loop

    payload = {
        "type": SINGLE_FREQUENCY_COMMAND_TYPE,
        "tag": CONTROL_TAG,
        "channel": 1,
        "value": 10,
        "mode": 2,
        "id": "closed-loop",
        "ts": time.time(),
    }
    cs._on_message(None, None, FakeMQTTMsg(json.dumps(payload).encode()))

    assert modbus.writes == 0
    ack = _last_ack(cs)
    assert ack["id"] == "closed-loop"
    assert ack["status"] == "success"


def test_control_ack_publishes(monkeypatch):
    cs, _ = _mk_sub(monkeypatch, require_sig=False, secret=None)
    msg_id = "abc"
    cs._ack(msg_id, CONTROL_TAG, "success", 10)

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

    config = _address_map_softpoint(address=1)
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

    subscriber._ack("ack-fail", CONTROL_TAG, "success", 10)

    failed_status = subscriber.get_status()
    assert failed_status["ready"] is False
    assert failed_status["reason"] == "ack_publish_failed"
    assert failed_status["ack_path"]["ok"] is False
    assert failed_status["ack_path"]["last_publish_rc"] == 1

    subscriber.client = FakeMQTTClient(rc=0)
    subscriber._ack("ack-ok", CONTROL_TAG, "success", 10)

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

    cs._on_message(None, None, FakeMQTTMsg(json.dumps({"tag": CONTROL_TAG, "value": 10, "id": "missing-ts"}).encode()))

    assert modbus.writes == 0
    ack = _last_ack(cs)
    assert ack["id"] == "missing-ts"
    assert ack["status"] == "nak"
    assert ack["reason"] == "missing_ts"


def test_control_rejects_out_of_range_with_nak(monkeypatch):
    monkeypatch.setenv("CONTROL_ENABLE_D5100_WRITER", "true")
    config = _address_map_softpoint(address=1)
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None, config=config)

    cs._on_message(
        None,
        None,
        FakeMQTTMsg(
            json.dumps(
                {
                    "type": SINGLE_FREQUENCY_COMMAND_TYPE,
                    "tag": CONTROL_TAG,
                    "channel": 1,
                    "value": 60,
                    "mode": 2,
                    "id": "oor",
                }
            ).encode()
        ),
    )

    assert modbus.writes == 0
    ack = _last_ack(cs)
    assert ack["id"] == "oor"
    assert ack["status"] == "nak"
    assert ack["reason"] == "value_out_of_range"
    assert ack["detail"] == {"range": [0, 50]}


def test_control_rejects_non_numeric_value_with_nak(monkeypatch):
    # 字符串 value 会绕过 isinstance 门控的范围校验，必须在写入前以 NAK 拒绝。
    monkeypatch.setenv("CONTROL_ENABLE_D5100_WRITER", "true")
    config = _address_map_softpoint(address=1)
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None, config=config)

    cs._on_message(
        None,
        None,
        FakeMQTTMsg(
            json.dumps(
                {
                    "type": SINGLE_FREQUENCY_COMMAND_TYPE,
                    "tag": CONTROL_TAG,
                    "channel": 1,
                    "value": "999",
                    "mode": 2,
                    "id": "str",
                }
            ).encode()
        ),
    )

    assert modbus.writes == 0
    ack = _last_ack(cs)
    assert ack["id"] == "str"
    assert ack["status"] == "nak"
    assert ack["reason"] == "value_not_numeric"


def test_control_rejects_nan_value_with_nak(monkeypatch):
    # NaN 的比较恒为 False，会穿透范围校验；必须在写入前以 NAK 拒绝。
    monkeypatch.setenv("CONTROL_ENABLE_D5100_WRITER", "true")
    config = _address_map_softpoint(address=1)
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None, config=config)

    # json.dumps(float("nan")) 默认输出 NaN 字面量，模拟 paho 收到的原始报文。
    cs._on_message(
        None,
        None,
        FakeMQTTMsg(
            json.dumps(
                {
                    "type": SINGLE_FREQUENCY_COMMAND_TYPE,
                    "tag": CONTROL_TAG,
                    "channel": 1,
                    "value": float("nan"),
                    "mode": 2,
                    "id": "nan",
                }
            ).encode()
        ),
    )

    assert modbus.writes == 0
    ack = _last_ack(cs)
    assert ack["id"] == "nan"
    assert ack["status"] == "nak"
    assert ack["reason"] == "value_not_numeric"


@pytest.mark.parametrize("bad_value", [True, float("inf"), float("-inf")])
def test_control_rejects_bool_and_infinite_values_with_nak(monkeypatch, bad_value):
    # bool 是 int 子类、±inf 非有限数；即便 tag 无 range，也必须在写入前 NAK，把行为钉死。
    monkeypatch.setenv("CONTROL_ENABLE_D5100_WRITER", "true")
    config = _address_map_softpoint(address=1)
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None, config=config)

    cs._on_message(
        None,
        None,
        FakeMQTTMsg(
            json.dumps(
                {
                    "type": SINGLE_FREQUENCY_COMMAND_TYPE,
                    "tag": CONTROL_TAG,
                    "channel": 1,
                    "value": bad_value,
                    "mode": 2,
                    "id": "bad",
                }
            ).encode()
        ),
    )

    assert modbus.writes == 0
    ack = _last_ack(cs)
    assert ack["id"] == "bad"
    assert ack["status"] == "nak"
    assert ack["reason"] == "value_not_numeric"


def test_control_rejects_acl_with_nak(monkeypatch):
    monkeypatch.setenv("CONTROL_ALLOWED_TAGS", "OTHER")
    monkeypatch.setenv("CONTROL_ENABLE_D5100_WRITER", "true")
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None)

    cs._on_message(
        None,
        None,
        FakeMQTTMsg(
            json.dumps(
                {
                    "type": SINGLE_FREQUENCY_COMMAND_TYPE,
                    "tag": CONTROL_TAG,
                    "channel": 1,
                    "value": 10,
                    "mode": 2,
                    "id": "acl",
                }
            ).encode()
        ),
    )

    assert modbus.writes == 0
    ack = _last_ack(cs)
    assert ack["id"] == "acl"
    assert ack["status"] == "nak"
    assert ack["reason"] == "tag_not_in_acl"


def test_legacy_frequency_tag_value_command_requires_d5100_transaction(monkeypatch):
    config = _address_map_softpoint(address=None)
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None, config=config)

    cs._on_message(None, None, FakeMQTTMsg(json.dumps({"tag": CONTROL_TAG, "value": 10, "id": "legacy"}).encode()))

    assert modbus.writes == 0
    ack = _last_ack(cs)
    assert ack["id"] == "legacy"
    assert ack["status"] == "nak"
    assert ack["reason"] == "single_frequency_transaction_required"


@pytest.mark.asyncio
async def test_control_subscriber_signed_single_frequency_transaction_acks(monkeypatch):
    monkeypatch.setenv("CONTROL_REQUIRE_SIGNATURE", "true")
    monkeypatch.setenv("CONTROL_REQUIRE_ID", "true")
    monkeypatch.setenv("CONTROL_REQUIRE_TIMESTAMP", "true")
    monkeypatch.setenv("CONTROL_SIGNATURE_SECRET", "secret")
    monkeypatch.setenv("CONTROL_ENABLE_D5100_WRITER", "true")
    subscriber = ControlSubscriber(FakeModbus(), {}, line_id="L1")
    subscriber.client = FakeMQTTClient()
    fake_writer = FakeFrequencyWriter(
        FrequencyTransactionResult(
            status="success",
            seq=41,
            channel=1,
            value=12,
            ack_seq=41,
            active_targets=[12, 0, 0, 0, 0, 0],
        )
    )
    subscriber.frequency_writer = fake_writer

    msg_id = "sim-ack-1"
    ts = time.time()
    value = 12
    payload = {
        "type": SINGLE_FREQUENCY_COMMAND_TYPE,
        "tag": CONTROL_TAG,
        "channel": 1,
        "value": value,
        "mode": 2,
        "id": msg_id,
        "ts": ts,
        "sig": _make_sig(
            "secret",
            msg_id,
            ts,
            CONTROL_TAG,
            value,
            command_type=SINGLE_FREQUENCY_COMMAND_TYPE,
            mode=2,
            channel=1,
        ),
    }
    subscriber._on_message(None, None, FakeMQTTMsg(json.dumps(payload).encode()))
    await asyncio.sleep(0)

    assert fake_writer.calls == [
        (
            1,
            12,
            {"mode": 2, "seq": None, "timeout_sec": 3.0, "refresh_heartbeat": False},
        )
    ]
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
async def test_control_subscriber_d5100_rate_limit_does_not_poison_id(monkeypatch):
    modbus = FakeModbus()
    monkeypatch.setenv("CONTROL_REQUIRE_ID", "false")
    monkeypatch.setenv("CONTROL_REQUIRE_TIMESTAMP", "false")
    monkeypatch.setenv("CONTROL_REQUIRE_SIGNATURE", "false")
    monkeypatch.setenv("CONTROL_ENABLE_D5100_WRITER", "true")

    cs = ControlSubscriber(modbus, {}, line_id="L1")
    cs.client = FakeMQTTClient()
    cs.min_interval_ms = 200  # 加快测试
    fake_writer = FakeFrequencyWriter(
        FrequencyTransactionResult(
            status="success",
            seq=1,
            channel=1,
            value=50,
            ack_seq=1,
            active_targets=[50, 0, 0, 0, 0, 0],
        )
    )
    cs.frequency_writer = fake_writer

    # 不启动真实 MQTT，直接调用 _on_message
    msg = FakeMsg(
        {
            "type": SINGLE_FREQUENCY_COMMAND_TYPE,
            "tag": CONTROL_TAG,
            "channel": 1,
            "value": 50,
            "mode": 2,
            "id": "i1",
            "ts": time.time(),
        }
    )
    cs._on_message(None, None, msg)
    await asyncio.sleep(0)  # 让 create_task 执行

    assert modbus.calls == []
    assert len(fake_writer.calls) == 1

    # 立即再次发送，因限流不应追加调用
    msg2 = FakeMsg(
        {
            "type": SINGLE_FREQUENCY_COMMAND_TYPE,
            "tag": CONTROL_TAG,
            "channel": 1,
            "value": 45,
            "mode": 2,
            "id": "i2",
            "ts": time.time(),
        }
    )
    cs._on_message(None, None, msg2)
    await asyncio.sleep(0)
    assert len(fake_writer.calls) == 1, "限流应阻止第二次写入"
    ack = _last_ack(cs)
    assert ack["id"] == "i2"
    assert ack["status"] == "nak"
    assert ack["reason"] == "rate_limited"

    # 等待超过限流窗口，同一 id 的限速重试应成功，而不是变成 replay。
    cs.last_sent_ts[CONTROL_TAG] = time.time() - 1
    cs._on_message(None, None, msg2)
    await asyncio.sleep(0)
    assert len(fake_writer.calls) == 2


def test_control_softpoint_from_address_map_tbd_rejected(monkeypatch):
    # 频率软点不再允许走中间层地址直写；必须使用 D5100 单路事务。
    config = _address_map_softpoint(address="TBD", scale="TBD")
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None, config=config)

    cs._on_message(
        None, None, FakeMQTTMsg(json.dumps({"tag": "VFM1_FREQ_SP", "value": 25, "id": "tbd"}).encode())
    )

    assert modbus.writes == 0
    ack = _last_ack(cs)
    assert ack["id"] == "tbd"
    assert ack["status"] == "nak"
    assert ack["reason"] == "single_frequency_transaction_required"


def test_control_softpoint_from_address_map_configured_still_rejects_legacy_direct_write(monkeypatch):
    # 即使地址表误配为 writable，也不能绕过 D5100 事务直接写寄存器。
    config = _address_map_softpoint(address=40100, scale=1.0)
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None, config=config)

    cs._on_message(
        None, None, FakeMQTTMsg(json.dumps({"tag": "VFM1_FREQ_SP", "value": 25.0, "id": "ok"}).encode())
    )

    assert modbus.writes == 0
    ack = _last_ack(cs)
    assert ack["status"] == "nak"
    assert ack["reason"] == "single_frequency_transaction_required"


def test_control_softpoint_from_address_map_out_of_range_rejected(monkeypatch):
    # D5100 事务入口仍受 edge-daq 独立 range guard（纵深防御）。
    monkeypatch.setenv("CONTROL_ENABLE_D5100_WRITER", "true")
    config = _address_map_softpoint(address=40100, scale=1.0)
    cs, modbus = _mk_sub(monkeypatch, require_sig=False, secret=None, config=config)

    cs._on_message(
        None,
        None,
        FakeMQTTMsg(
            json.dumps(
                {
                    "type": SINGLE_FREQUENCY_COMMAND_TYPE,
                    "tag": "VFM1_FREQ_SP",
                    "channel": 1,
                    "value": 75,
                    "mode": 2,
                    "id": "hi",
                }
            ).encode()
        ),
    )

    assert modbus.writes == 0
    ack = _last_ack(cs)
    assert ack["status"] == "nak"
    assert ack["reason"] == "value_out_of_range"


def test_control_rejects_unmapped_or_bad_tag(monkeypatch):
    modbus = FakeModbus()
    monkeypatch.setenv("CONTROL_REQUIRE_ID", "false")
    monkeypatch.setenv("CONTROL_REQUIRE_TIMESTAMP", "false")
    monkeypatch.setenv("CONTROL_REQUIRE_SIGNATURE", "false")
    cs = ControlSubscriber(modbus, {}, line_id="L1")
    cs.client = FakeMQTTClient()

    # 未知 tag
    cs._on_message(None, None, FakeMsg({"tag": "UNKNOWN", "value": 1, "id": "unknown"}))
    assert not modbus.calls
    ack = _last_ack(cs)
    assert ack["id"] == "unknown"
    assert ack["status"] == "nak"
    assert ack["reason"] == "tag_not_found"

    # 频率软点必须走 D5100 事务，不允许旧 tag/value 入口。
    cs._on_message(None, None, FakeMsg({"tag": CONTROL_TAG, "value": 1, "id": "unmapped"}))
    assert not modbus.calls
    ack = _last_ack(cs)
    assert ack["id"] == "unmapped"
    assert ack["status"] == "nak"
    assert ack["reason"] == "single_frequency_transaction_required"
