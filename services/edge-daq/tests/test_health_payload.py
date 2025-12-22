import importlib
import time


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


class FakeReader:
    def __init__(self, scan_interval):
        self.scan_interval = scan_interval


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

