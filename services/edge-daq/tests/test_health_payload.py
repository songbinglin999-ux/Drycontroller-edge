import importlib
import time

import pytest


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
                            "name": "SIM_FREQ_SP",
                            "address": 40100,
                            "signal_type": "AO",
                            "type": "float",
                            "control_enabled": True,
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
