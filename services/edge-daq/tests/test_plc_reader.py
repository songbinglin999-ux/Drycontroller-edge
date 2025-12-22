import asyncio
import pytest

from daq.plc_reader import PLCReader
from daq.modbus_client import PLC_HEARTBEAT_MISSING


class FakeModbus:
    def __init__(self, float_value=10.0, int_value=2):
        self.float_value = float_value
        self.int_value = int_value

    async def read_float(self, address, byte_order="big", function_code=3):
        return self.float_value

    async def read_int32(self, address, signed=True, byte_order="big", function_code=3):
        return int(self.float_value)

    async def read_registers(self, address, count=1, function_code=3):
        return [self.int_value] * count


class FakePublisher:
    def __init__(self):
        self.published = []
        self.alarms = []
        self.connected = True

    async def publish_tag_data(self, tag_name, value, unit, timestamp):
        self.published.append({"tag": tag_name, "value": value, "unit": unit})

    async def publish_alarm(self, tag_name, alarm_type, value, threshold):
        self.alarms.append(
            {
                "tag": tag_name,
                "type": alarm_type,
                "value": value,
                "threshold": threshold,
            }
        )


@pytest.mark.asyncio
async def test_plc_reader_scale_and_alarm():
    modbus = FakeModbus(float_value=10.0)
    publisher = FakePublisher()
    config = {
        "acquisition": {"scan_interval": 0.1},
        "tags": {
            "section": {
                "category": [
                    {
                        "name": "TEST.FLOAT",
                        "address": 40001,
                        "type": "float",
                        "unit": "°C",
                        "scale": 0.1,
                        "function_code": 3,
                        "byte_order": "big",
                        "alarm_high": 0.5,  # 读取后会缩放 10 * 0.1 = 1.0 > 0.5
                    }
                ]
            }
        },
    }

    reader = PLCReader(modbus, publisher, config)

    await reader._read_tag(config["tags"]["section"]["category"][0])

    assert publisher.published, "数据应被发布"
    published = publisher.published[0]
    assert published["tag"] == "TEST.FLOAT"
    assert pytest.approx(published["value"], rel=1e-5) == 1.0

    # 报警被触发
    assert publisher.alarms
    alarm = publisher.alarms[0]
    assert alarm["tag"] == "TEST.FLOAT"
    assert alarm["type"] == "high"


@pytest.mark.asyncio
async def test_plc_reader_int32_and_function_code():
    modbus = FakeModbus(float_value=123.4, int_value=7)
    publisher = FakePublisher()
    config = {
        "tags": {
            "section": {
                "category": [
                    {
                        "name": "TEST.INT32",
                        "address": 41000,
                        "type": "int32",
                        "unit": "raw",
                        "scale": 1,
                        "function_code": 4,
                        "byte_order": "little",
                    }
                ]
            }
        }
    }

    reader = PLCReader(modbus, publisher, config)

    await reader._read_tag(config["tags"]["section"]["category"][0])

    assert publisher.published
    assert publisher.published[0]["tag"] == "TEST.INT32"


@pytest.mark.asyncio
async def test_plc_reader_bool_and_function_code4():
    modbus = FakeModbus(float_value=0.0, int_value=1)
    publisher = FakePublisher()
    config = {
        "tags": {
            "section": {
                "category": [
                    {
                        "name": "TEST.BOOL",
                        "address": 42000,
                        "type": "bool",
                        "unit": "",
                        "function_code": 4,
                    }
                ]
            }
        }
    }

    reader = PLCReader(modbus, publisher, config)

    await reader._read_tag(config["tags"]["section"]["category"][0])

    assert publisher.published
    assert publisher.published[0]["tag"] == "TEST.BOOL"
    assert publisher.published[0]["value"] in (True, False)


class BatchModbus:
    def __init__(self, registers):
        # registers: dict address -> value
        self.registers = registers
        self.calls = []

    async def read_registers(self, address, count, function_code=3):
        self.calls.append((address, count, function_code))
        vals = []
        for i in range(count):
            vals.append(self.registers.get(address + i, 0))
        return vals


@pytest.mark.asyncio
async def test_plc_reader_batch_reduces_calls():
    # addresses 100,101,102 连续，可合并为一次 read_registers(count=3)
    modbus = BatchModbus({100: 10, 101: 20, 102: 30})
    publisher = FakePublisher()
    config = {
        "acquisition": {"batch_size": 10},
        "tags": {
            "s": {
                "c": [
                    {"name": "T1", "address": 100, "type": "int", "unit": ""},
                    {"name": "T2", "address": 101, "type": "int", "unit": ""},
                    {"name": "T3", "address": 102, "type": "int", "unit": ""},
                ]
            }
        },
    }

    reader = PLCReader(modbus, publisher, config)
    await reader._read_all_tags()

    # 只应发起一次批量读取
    assert len(modbus.calls) == 1
    assert publisher.published[0]["value"] == 10
    assert publisher.published[1]["value"] == 20
    assert publisher.published[2]["value"] == 30


@pytest.mark.asyncio
async def test_plc_reader_heartbeat_missing(monkeypatch):
    # 模拟心跳寄存器不变化，触发缺失计数
    class HBModbus:
        def __init__(self):
            self.counter = 0
        async def read_registers(self, address, count, function_code=3):
            self.counter += 1
            return [1]  # 固定不变
        async def write_register(self, address, value):
            return True

    modbus = HBModbus()
    publisher = FakePublisher()
    config = {
        "heartbeat": {"read_address": 50000, "write_address": 50001, "function_code": 3},
    }
    svc_state = type("S", (), {"heartbeat_miss_count": 0, "heartbeat_fail_count": 0})()
    reader = PLCReader(modbus, publisher, config, heartbeat_interval=0.01, heartbeat_timeout_cycles=2, heartbeat_state=svc_state)

    # 触发多次心跳循环
    for _ in range(4):
        await reader._heartbeat_cycle()

    assert svc_state.heartbeat_miss_count >= 2

