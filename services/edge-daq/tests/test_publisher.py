import base64
import json
import time

import pytest

from daq.publisher import (
    DataPublisher,
    MQTT_BUFFER_OLDEST_AGE_SEC,
    INFLUX_BUFFER_OLDEST_AGE_SEC,
    INFLUX_TIMESTAMP_PARSE_FAILURE,
)


class FakeMQTTClient:
    def __init__(self, rc=0):
        self.rc = rc
        self.published = []

    def publish(self, topic, payload, qos):
        self.published.append((topic, payload, qos))

        class R:
            def __init__(self, rc):
                self.rc = rc

        return R(self.rc)


class FakeWriteAPI:
    def __init__(self):
        self.records = []

    def write(self, bucket, org, record):
        self.records.append((bucket, org, record))


@pytest.mark.asyncio
async def test_mqtt_buffer_persist_and_flush(monkeypatch, tmp_path):
    monkeypatch.setenv("BUFFER_DIR", str(tmp_path))
    monkeypatch.setenv("DEAD_LETTER_DIR", str(tmp_path / "deadletter"))
    pub = DataPublisher()
    pub.mqtt_client = FakeMQTTClient()
    pub.persist_mqtt_buffer = True

    pub._buffer_mqtt("t", {"a": 1}, qos=1)
    assert len(pub.mqtt_buffer) == 1
    assert MQTT_BUFFER_OLDEST_AGE_SEC._value.get() >= 0

    await pub._flush_mqtt_buffer()
    assert len(pub.mqtt_buffer) == 0
    assert len(pub.mqtt_client.published) == 1

    # 重建实例应加载持久化缓冲（为空时不报错）
    pub2 = DataPublisher()
    pub2.persist_mqtt_buffer = True
    assert len(pub2.mqtt_buffer) >= 0


@pytest.mark.asyncio
async def test_influx_buffer_persist_and_flush(monkeypatch, tmp_path):
    monkeypatch.setenv("BUFFER_DIR", str(tmp_path))
    monkeypatch.setenv("DEAD_LETTER_DIR", str(tmp_path / "deadletter"))
    pub = DataPublisher()
    pub.persist_influx_buffer = True
    fake_write = FakeWriteAPI()
    pub.write_api = fake_write

    pub._buffer_influx("tag", 1.0, "u", time.time())
    assert len(pub.influx_buffer) == 1
    assert pub.influx_buffer[0]["quality"] == "good"
    assert INFLUX_BUFFER_OLDEST_AGE_SEC._value.get() >= 0

    await pub._flush_influx_buffer()
    assert len(pub.influx_buffer) == 0
    assert len(fake_write.records) == 1


@pytest.mark.asyncio
async def test_persisted_influx_requires_latest_quality_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("BUFFER_DIR", str(tmp_path))
    monkeypatch.setenv("DEAD_LETTER_DIR", str(tmp_path / "deadletter"))
    valid = {
        "tag": "current",
        "value": 2.0,
        "unit": "A",
        "quality": "good",
        "ts_ns": time.time_ns(),
    }
    legacy = {"tag": "legacy", "value": 1.0, "unit": "A", "ts_ns": time.time_ns()}
    malformed = '{"tag":"torn"'
    (tmp_path / "influx_buffer.jsonl").write_text(
        f"{json.dumps(legacy)}\n{malformed}\n{json.dumps(valid)}\n",
        encoding="utf-8",
    )

    pub = DataPublisher()
    pub.write_api = FakeWriteAPI()

    assert list(pub.influx_buffer) == [valid]
    dead_letters = [
        json.loads(line)
        for line in pub.influx_dead_letter.read_text(encoding="utf-8").splitlines()
    ]
    assert dead_letters == [
        {"reason": "invalid_persisted_buffer_schema", "entry": legacy},
        {"reason": "invalid_persisted_buffer_json", "entry": malformed},
    ]
    await pub._flush_influx_buffer()
    assert not pub.influx_buffer
    assert len(pub.write_api.records) == 1


@pytest.mark.parametrize(
    ("buffer_type", "invalid_line", "valid_entry"),
    [
        (
            "mqtt",
            b'{"topic":"t","data":{"label":"\xe4\xb8',
            {"topic": "valid", "data": {"value": 1}, "qos": 1, "ts": 2},
        ),
        (
            "influx",
            b'{"tag":"\xe4\xb8',
            {
                "tag": "valid",
                "value": 2.0,
                "unit": "C",
                "quality": "good",
                "ts_ns": 2,
            },
        ),
    ],
)
def test_persisted_buffer_invalid_utf8_deadletters_raw_line(
    monkeypatch, tmp_path, buffer_type, invalid_line, valid_entry
):
    monkeypatch.setenv("BUFFER_DIR", str(tmp_path))
    monkeypatch.setenv("DEAD_LETTER_DIR", str(tmp_path / "deadletter"))
    buffer_file = tmp_path / f"{buffer_type}_buffer.jsonl"
    buffer_file.write_bytes(json.dumps(valid_entry).encode("utf-8") + b"\n" + invalid_line)

    pub = DataPublisher()

    assert list(getattr(pub, f"{buffer_type}_buffer")) == [valid_entry]
    dead_letter_file = getattr(pub, f"{buffer_type}_dead_letter")
    dead_letters = [
        json.loads(line) for line in dead_letter_file.read_text(encoding="utf-8").splitlines()
    ]
    assert dead_letters == [
        {
            "reason": "invalid_persisted_buffer_utf8",
            "raw_line_base64": base64.b64encode(invalid_line).decode("ascii"),
        }
    ]
    assert base64.b64decode(dead_letters[0]["raw_line_base64"], validate=True) == invalid_line


@pytest.mark.parametrize("buffer_type", ["mqtt", "influx"])
def test_persisted_buffer_deadletter_failure_is_fail_closed(monkeypatch, tmp_path, buffer_type):
    monkeypatch.setenv("BUFFER_DIR", str(tmp_path))
    dead_letter_dir = tmp_path / "deadletter"
    monkeypatch.setenv("DEAD_LETTER_DIR", str(dead_letter_dir))
    dead_letter_dir.mkdir()
    (dead_letter_dir / f"{buffer_type}_deadletter.jsonl").mkdir()
    invalid_line = b'{"label":"\xe4\xb8'
    buffer_file = tmp_path / f"{buffer_type}_buffer.jsonl"
    buffer_file.write_bytes(invalid_line)

    with pytest.raises(RuntimeError, match="dead-letter persistence failed"):
        DataPublisher()

    assert buffer_file.read_bytes() == invalid_line


@pytest.mark.asyncio
async def test_mqtt_auth_required(monkeypatch, tmp_path):
    monkeypatch.setenv("MQTT_REQUIRE_AUTH", "true")
    monkeypatch.setenv("MQTT_TLS", "true")
    monkeypatch.setenv("BUFFER_DIR", str(tmp_path))
    monkeypatch.setenv("DEAD_LETTER_DIR", str(tmp_path / "deadletter"))
    pub = DataPublisher()
    pub.mqtt_user = ""
    pub.mqtt_password = ""
    await pub._connect_mqtt()
    assert pub.connected is False
    assert pub.last_mqtt_error


@pytest.mark.asyncio
async def test_publish_tag_data_buffers_when_mqtt_not_ready(monkeypatch, tmp_path):
    """
    工业现场 MQTT 未就绪（断网/未连上 broker）时，采集数据不应丢弃，应进入缓冲并可落盘重放。
    """
    monkeypatch.setenv("BUFFER_DIR", str(tmp_path))
    monkeypatch.setenv("DEAD_LETTER_DIR", str(tmp_path / "deadletter"))

    pub = DataPublisher()
    # 模拟 MQTT 未就绪 & Influx 未就绪
    pub.mqtt_client = None
    pub.write_api = None

    await pub.publish_tag_data(tag_name="T", value=1.23, unit="u", timestamp=time.time())

    assert len(pub.mqtt_buffer) == 1
    assert len(pub.influx_buffer) == 1
    assert pub.mqtt_buffer[0]["data"]["quality"] == "good"
    assert pub.influx_buffer[0]["quality"] == "good"


def test_backpressure_failure_returns_max_delay(monkeypatch, tmp_path):
    monkeypatch.setenv("BUFFER_DIR", str(tmp_path))
    monkeypatch.setenv("DEAD_LETTER_DIR", str(tmp_path / "deadletter"))
    monkeypatch.setenv("BACKPRESSURE_MAX_DELAY_SEC", "0.25")
    pub = DataPublisher()
    pub.mqtt_buffer = None

    delay = pub.get_backpressure_delay()

    assert delay == pytest.approx(0.25)
    assert pub.last_backpressure_error


def test_persist_buffer_disables_on_statvfs_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("BUFFER_DIR", str(tmp_path))
    monkeypatch.setenv("DEAD_LETTER_DIR", str(tmp_path / "deadletter"))
    monkeypatch.setenv("BUFFER_MIN_FREE_BYTES", "1")
    pub = DataPublisher()

    def fail_statvfs(path):
        raise OSError("stat failed")

    monkeypatch.setattr("daq.publisher.os.statvfs", fail_statvfs)

    pub._persist_buffer(pub.mqtt_buffer_file, {"a": 1}, enable=True)

    assert pub._persist_disabled["mqtt"] is True
    assert pub.last_buffer_error == "stat failed"
    assert not pub.mqtt_buffer_file.exists()


@pytest.mark.asyncio
async def test_publish_tag_data_rejects_invalid_timestamp(monkeypatch, tmp_path):
    monkeypatch.setenv("BUFFER_DIR", str(tmp_path))
    monkeypatch.setenv("DEAD_LETTER_DIR", str(tmp_path / "deadletter"))
    pub = DataPublisher()

    with pytest.raises(ValueError, match="invalid timestamp"):
        await pub.publish_tag_data(tag_name="T", value=1.23, unit="u", timestamp="not-a-time")

    assert pub.last_influx_error == "invalid_timestamp: 'not-a-time'"
    assert len(pub.mqtt_buffer) == 0
    assert len(pub.influx_buffer) == 0


def test_influx_entry_ts_sec_records_bad_ts_ns_and_uses_legacy_ts(monkeypatch, tmp_path):
    monkeypatch.setenv("BUFFER_DIR", str(tmp_path))
    monkeypatch.setenv("DEAD_LETTER_DIR", str(tmp_path / "deadletter"))
    pub = DataPublisher()
    before = INFLUX_TIMESTAMP_PARSE_FAILURE.labels("ts_ns")._value.get()

    ts = pub._influx_entry_ts_sec({"ts_ns": "bad-ns", "ts": 123.5})

    assert ts == pytest.approx(123.5)
    assert pub.last_influx_error == "invalid_buffer_timestamp_ts_ns: 'bad-ns'"
    assert INFLUX_TIMESTAMP_PARSE_FAILURE.labels("ts_ns")._value.get() == pytest.approx(before + 1)


def test_influx_entry_ts_sec_records_bad_ts_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("BUFFER_DIR", str(tmp_path))
    monkeypatch.setenv("DEAD_LETTER_DIR", str(tmp_path / "deadletter"))
    pub = DataPublisher()
    before = INFLUX_TIMESTAMP_PARSE_FAILURE.labels("ts")._value.get()
    monkeypatch.setattr("daq.publisher.time.time", lambda: 456.0)

    ts = pub._influx_entry_ts_sec({"ts": "bad-ts"})

    assert ts == pytest.approx(456.0)
    assert pub.last_influx_error == "invalid_buffer_timestamp_ts: 'bad-ts'"
    assert INFLUX_TIMESTAMP_PARSE_FAILURE.labels("ts")._value.get() == pytest.approx(before + 1)
