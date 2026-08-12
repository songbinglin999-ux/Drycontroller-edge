import asyncio
import time

import pytest
from pymodbus.transaction import ModbusRtuFramer

from daq.modbus_client import ModbusClient


class FakeResult:
    def __init__(self, registers=None, error=False):
        self.registers = registers or []
        self._error = error

    def isError(self):
        return self._error


class FakeClient:
    def __init__(self):
        self.connected = True
        self.read_calls = 0
        self.write_calls = 0
        self.mode = "ok"  # ok | error | exception
        self.mode_sequence = []  # 优先使用序列

    async def read_holding_registers(self, address, count, slave):
        self.read_calls += 1
        if self.mode_sequence:
            self.mode = self.mode_sequence.pop(0)
        if self.mode == "error":
            return FakeResult(error=True)
        if self.mode == "exception":
            raise RuntimeError("read boom")
        return FakeResult(registers=[1] * count)

    async def write_register(self, address, value, slave):
        self.write_calls += 1
        if self.mode == "error":
            return FakeResult(error=True)
        if self.mode == "exception":
            raise RuntimeError("write boom")
        return FakeResult()

    async def write_registers(self, address, values, slave):
        self.write_calls += 1
        if self.mode == "error":
            return FakeResult(error=True)
        if self.mode == "exception":
            raise RuntimeError("write boom")
        return FakeResult()

    async def read_input_registers(self, address, count, slave):
        return await self.read_holding_registers(address, count, slave)

    def close(self):
        self.connected = False


def test_modbus_defaults_to_current_rtu_over_tcp_contract(monkeypatch):
    monkeypatch.delenv("MODBUS_FRAMER", raising=False)

    client = ModbusClient("127.0.0.1")

    assert client.port == 2022
    assert client.slave_id == 1
    assert client.framer is ModbusRtuFramer


@pytest.mark.asyncio
async def test_modbus_read_retry_and_success(monkeypatch):
    mc = ModbusClient("127.0.0.1", port=1502, slave_id=2)
    fake = FakeClient()
    mc.client = fake
    mc.connected = True

    # 首次 error，第二次成功
    fake.mode_sequence = ["error", "ok"]

    async def ensure_connected():
        return True

    monkeypatch.setattr(mc, "ensure_connected", ensure_connected)

    registers = await mc.read_registers(1, 2, function_code=3, retries=2)

    assert registers == [1, 1]
    assert fake.read_calls >= 2
    assert mc.failure_count >= 1
    assert mc.last_success_ts is not None


@pytest.mark.asyncio
async def test_modbus_write_retry_on_exception(monkeypatch):
    mc = ModbusClient("127.0.0.1", port=1502, slave_id=1)
    fake = FakeClient()
    mc.client = fake
    mc.connected = True
    fake.mode = "exception"

    async def ensure_connected():
        return True

    monkeypatch.setattr(mc, "ensure_connected", ensure_connected)

    # 让第二次变成功
    async def flip_mode():
        await asyncio.sleep(0)
        fake.mode = "ok"

    asyncio.create_task(flip_mode())

    ok = await mc.write_register(10, 5, retries=2)
    assert ok is True
    assert fake.write_calls >= 2
    assert mc.failure_count >= 1


def test_modbus_status_payload():
    mc = ModbusClient("127.0.0.1", port=1502, slave_id=3)
    mc.connected = True
    mc.last_success_ts = time.time()
    status = mc.get_status()
    assert status["connected"] is True
    assert status["slave_id"] == 3
