import time
import asyncio

import pytest

from daq.publisher import (
    DataPublisher,
    MQTT_BUFFER_OLDEST_AGE_SEC,
    INFLUX_BUFFER_OLDEST_AGE_SEC,
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
    pub = DataPublisher()
    pub.persist_influx_buffer = True
    fake_write = FakeWriteAPI()
    pub.write_api = fake_write

    pub._buffer_influx("tag", 1.0, "u", time.time())
    assert len(pub.influx_buffer) == 1
    assert INFLUX_BUFFER_OLDEST_AGE_SEC._value.get() >= 0

    await pub._flush_influx_buffer()
    assert len(pub.influx_buffer) == 0
    assert len(fake_write.records) == 1


@pytest.mark.asyncio
async def test_mqtt_auth_required(monkeypatch, tmp_path):
    monkeypatch.setenv("MQTT_REQUIRE_AUTH", "true")
    monkeypatch.setenv("MQTT_TLS", "true")
    monkeypatch.setenv("BUFFER_DIR", str(tmp_path))
    pub = DataPublisher()
    pub.mqtt_user = ""
    pub.mqtt_password = ""
    await pub._connect_mqtt()
    assert pub.connected is False
    assert pub.last_mqtt_error

