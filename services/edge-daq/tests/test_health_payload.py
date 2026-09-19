import importlib
import time
from pathlib import Path

import pytest
import yaml


class FakeModbus:
    def __init__(self):
        self.status = {
            "connected": True,
            "last_success_ts": time.time(),
            "failure_count": 0,
            "reconnect_count": 1,
        }

    def get_status(self):
        return self.status


class FakePublisher:
    def __init__(self):
        self.connected = True
        self.line_id = "L1"


class ReadyPublisher:
    line_id = "L1"
    mqtt_connected = True
    influx_connected = False

    def get_status(self):
        return {"mqtt_connected": True, "influx_connected": False}


class FakeReader:
    def __init__(self, scan_interval):
        self.scan_interval = scan_interval


class FakeControlSubscriber:
    def __init__(self, status):
        self.status = status

    def get_status(self):
        return self.status


@pytest.mark.asyncio
async def test_load_config_derives_v0_heartbeat_from_modbus_map(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))

    tags_path = tmp_path / "tags.yaml"
    address_map_path = tmp_path / "modbus-address-map.yaml"
    tags_path.write_text("tags: {}\n", encoding="utf-8")
    address_map_path.write_text(
        "\n".join(
            [
                "PLC_HEARTBEAT: {address: 45000, function_code: 3, scale: 1, byte_order: big, type: int, diagnostic: heartbeat}",
                "EDGE_HOST_ALIVE: {address: 45001, function_code: 6, scale: 1, byte_order: big, writable: true, type: int, diagnostic: heartbeat, control_softpoint: false}",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TAGS_CONFIG", str(tags_path))
    monkeypatch.setenv("MODBUS_ADDRESS_MAP", str(address_map_path))

    daq_main = importlib.import_module("daq.main")
    svc = daq_main.DAQService()

    config = await svc.load_config()

    assert config["heartbeat"] == {
        "read_address": 45000,
        "write_address": 45001,
        "function_code": 3,
    }


@pytest.mark.asyncio
async def test_load_config_skips_v0_heartbeat_until_addresses_are_signed(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))

    tags_path = tmp_path / "tags.yaml"
    address_map_path = tmp_path / "modbus-address-map.yaml"
    tags_path.write_text("tags: {}\nheartbeat: {read_address: 1, write_address: 2}\n", encoding="utf-8")
    address_map_path.write_text(
        "\n".join(
            [
                "PLC_HEARTBEAT: {address: TBD, function_code: 3, scale: 1, byte_order: big, type: int, diagnostic: heartbeat}",
                "EDGE_HOST_ALIVE: {address: TBD, function_code: 6, scale: 1, byte_order: big, writable: true, type: int, diagnostic: heartbeat, control_softpoint: false}",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TAGS_CONFIG", str(tags_path))
    monkeypatch.setenv("MODBUS_ADDRESS_MAP", str(address_map_path))

    daq_main = importlib.import_module("daq.main")
    svc = daq_main.DAQService()

    config = await svc.load_config()

    assert "heartbeat" not in config


def test_real_modbus_map_enables_v0_heartbeat_and_keeps_v1_sp_fail_closed():
    daq_main = importlib.import_module("daq.main")
    repo_root = Path(__file__).resolve().parents[3]
    address_map = yaml.safe_load((repo_root / "configs/modbus-address-map.yaml").read_text(encoding="utf-8"))

    assert daq_main._derive_heartbeat_config(address_map) == {
        "read_address": 45290,
        "write_address": 45291,
        "function_code": 3,
    }
    assert address_map["PLC_HEARTBEAT"]["diagnostic"] == "heartbeat"
    assert address_map["EDGE_HOST_ALIVE"]["diagnostic"] == "heartbeat"
    assert address_map["EDGE_HOST_ALIVE"]["control_softpoint"] is False

    for tag in (
        "VFM1_FREQ_SP",
        "VFM2_FREQ_SP",
        "VFM3_FREQ_SP",
        "VF3_FREQ_SP",
    ):
        assert address_map[tag]["address"] == "TBD"
        assert address_map[tag]["writable"] is False


def test_health_payload_includes_modbus_and_scan(tmp_path, monkeypatch):
    # 避免在 /logs 下写文件，重定向日志根目录
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))

    # 延迟导入，应用新的 LOG_ROOT
    daq_main = importlib.import_module("daq.main")
    DAQService = daq_main.DAQService

    svc = DAQService()
    svc.running = True
    svc.publisher = FakePublisher()
    svc.modbus_client = FakeModbus()
    svc.plc_reader = FakeReader(scan_interval=0.5)

    payload = svc._health_payload()

    assert payload["details"]["publisher_connected"] is True
    assert payload["details"]["modbus"]["connected"] is True
    assert payload["details"]["scan_interval"] == 0.5


def test_ready_payload_reports_control_start_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))

    daq_main = importlib.import_module("daq.main")
    DAQService = daq_main.DAQService

    svc = DAQService()
    svc.running = True
    svc.publisher = ReadyPublisher()
    svc.simulator = object()
    svc.control_subscriber = FakeControlSubscriber(
        {
            "ready": False,
            "reason": "start_failed",
            "start_error": "MQTT TLS is required",
            "subscribe_state": "start_failed",
            "ack_path": {"ok": None},
        }
    )

    payload = svc._ready_payload()

    assert payload["ready"] is False
    assert payload["reason"] == "CONTROL_SUBSCRIBER_NOT_READY"
    assert payload["details"]["control"]["reason"] == "start_failed"
    assert payload["details"]["control"]["start_error"] == "MQTT TLS is required"


def test_ready_payload_tracks_control_subscribe_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))

    daq_main = importlib.import_module("daq.main")
    DAQService = daq_main.DAQService

    control_status = {
        "ready": False,
        "reason": "subscribe_failed",
        "mqtt_connected": True,
        "subscribed": False,
        "subscribe_state": "failed",
        "ack_path": {"ok": None},
    }
    svc = DAQService()
    svc.running = True
    svc.publisher = ReadyPublisher()
    svc.simulator = object()
    svc.control_subscriber = FakeControlSubscriber(control_status)

    failed_payload = svc._ready_payload()

    assert failed_payload["ready"] is False
    assert failed_payload["reason"] == "CONTROL_SUBSCRIBER_NOT_READY"
    assert failed_payload["details"]["control"]["subscribe_state"] == "failed"

    control_status.update(
        {
            "ready": True,
            "reason": None,
            "mqtt_connected": True,
            "subscribed": True,
            "subscribe_state": "subscribed",
            "ack_path": {"ok": True, "last_status": "success"},
        }
    )

    recovered_payload = svc._ready_payload()

    assert recovered_payload["ready"] is True
    assert recovered_payload["reason"] is None
    assert recovered_payload["details"]["control"]["ack_path"]["ok"] is True


def test_ready_payload_reports_ack_path_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))

    daq_main = importlib.import_module("daq.main")
    DAQService = daq_main.DAQService

    svc = DAQService()
    svc.running = True
    svc.publisher = ReadyPublisher()
    svc.simulator = object()
    svc.control_subscriber = FakeControlSubscriber(
        {
            "ready": False,
            "reason": "ack_publish_failed",
            "mqtt_connected": True,
            "subscribed": True,
            "subscribe_state": "subscribed",
            "ack_path": {
                "ok": False,
                "last_publish_rc": 1,
                "last_status": "success",
            },
        }
    )

    payload = svc._ready_payload()

    assert payload["ready"] is False
    assert payload["reason"] == "CONTROL_SUBSCRIBER_NOT_READY"
    assert payload["details"]["control"]["reason"] == "ack_publish_failed"
    assert payload["details"]["control"]["ack_path"]["last_publish_rc"] == 1


@pytest.mark.asyncio
async def test_simulator_mode_starts_control_subscriber(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))
    monkeypatch.setenv("USE_SIMULATOR", "true")

    daq_main = importlib.import_module("daq.main")
    DAQService = daq_main.DAQService

    class StubPublisher:
        line_id = "L1"
        connected = True
        mqtt_connected = True
        influx_connected = False

        async def connect(self):
            return None

    class StubReader:
        scan_interval = 0.1

        def __init__(self, modbus_client, publisher, config, **kwargs):
            self.modbus_client = modbus_client

        async def start(self):
            return None

    started = {}

    class StubControlSubscriber:
        def __init__(self, modbus_client, config, line_id):
            started["modbus_client"] = modbus_client
            started["line_id"] = line_id

        async def start(self):
            started["started"] = True

    async def no_health_server(self):
        return None

    monkeypatch.setattr(daq_main, "DataPublisher", StubPublisher)
    monkeypatch.setattr(daq_main, "PLCReader", StubReader)
    monkeypatch.setattr(daq_main, "ControlSubscriber", StubControlSubscriber)
    monkeypatch.setattr(DAQService, "_start_health_server", no_health_server)

    svc = DAQService()

    async def load_config():
        svc.config = {
            "tags": {
                "s": {
                    "c": [
                        {
                            "name": "VFM1_FREQ_SP",
                            "address": 40100,
                            "signal_type": "AO",
                            "type": "float",
                        }
                    ]
                }
            }
        }
        return svc.config

    monkeypatch.setattr(svc, "load_config", load_config)

    await svc.start()

    assert svc.simulator is not None
    assert started["modbus_client"] is svc.simulator
    assert started["line_id"] == "L1"
    assert started["started"] is True


@pytest.mark.asyncio
async def test_proxy_runtime_never_starts_legacy_subscriber(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    daq_main = importlib.import_module('daq.main')
    monkeypatch.setenv('USE_SIMULATOR', 'false')
    monkeypatch.setenv('MODBUS_FRAMER', 'socket')
    monkeypatch.setenv('CONTROL_ENABLE_D5100_WRITER', 'false')
    monkeypatch.setattr(daq_main, 'load_proxy_config', lambda: {})
    publisher = ReadyPublisher()
    publisher.connect = AsyncMock()
    publisher.disconnect = AsyncMock()
    publisher.flush_buffers = AsyncMock()
    monkeypatch.setattr(daq_main, 'DataPublisher', lambda: publisher)
    client = FakeModbus()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    monkeypatch.setattr(daq_main, 'ModbusClient', lambda **kwargs: client)
    reader = Mock(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(daq_main, 'PLCReader', lambda *args, **kwargs: reader)
    proxy = Mock(start=AsyncMock(), stop=AsyncMock())
    proxy.get_status.return_value = {'ready': True, 'mode': 'modbus_tcp_proxy'}
    monkeypatch.setattr(daq_main, 'ModbusProxy', lambda *args, **kwargs: proxy)
    subscriber = Mock(side_effect=AssertionError('legacy physical writer must not start'))
    monkeypatch.setattr(daq_main, 'ControlSubscriber', subscriber)
    svc = daq_main.DAQService()
    async def load_config():
        svc.config = {'tags': {}}
    monkeypatch.setattr(svc, 'load_config', load_config)
    monkeypatch.setattr(svc, '_start_health_server', AsyncMock())
    await svc.start()
    assert svc.control_subscriber is None
    assert svc._control_status()['mode'] == 'modbus_tcp_proxy'
    proxy.start.assert_awaited_once()
    await svc.stop()
    proxy.stop.assert_awaited_once()
    client.disconnect.assert_awaited_once()
