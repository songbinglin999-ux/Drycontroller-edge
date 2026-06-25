import asyncio
import contextlib
import os
from pathlib import Path
import socket
import sys

import pytest

from daq.frequency_transaction import (
    D_MODBUS_BASE,
    EDGE_FREQ_CHANNEL,
    EDGE_FREQ_VALUE,
    EDGE_REMOTE_RUN_START,
    EDGE_REQ_CHECK,
    EDGE_REQ_SEQ,
    EDGE_REQ_TYPE,
    PLC_ACTIVE_FREQ_TARGET_START,
    PLC_LAST_ACK_SEQ,
    SingleFrequencyTransactionWriter,
)
from daq.modbus_client import ModbusClient


def _addr(d_number: int) -> int:
    return D_MODBUS_BASE + d_number


class FakeTransactionModbus:
    def __init__(self, *, readback_targets=None):
        self.registers = {}
        self.writes = []
        self.readback_targets = readback_targets

    async def read_registers(self, address: int, count: int = 1, function_code: int = 3, retries: int = 2):
        return [self.registers.get(address + offset, 0) for offset in range(count)]

    async def write_registers(self, address: int, values: list[int], retries: int = 2):
        self.writes.append(("multi", address, list(values)))
        for offset, value in enumerate(values):
            self.registers[address + offset] = int(value)
        return True

    async def write_register(self, address: int, value: int, retries: int = 2):
        self.writes.append(("single", address, int(value)))
        self.registers[address] = int(value)
        if address == _addr(EDGE_REQ_SEQ):
            self.registers[_addr(PLC_LAST_ACK_SEQ)] = int(value)
            targets = self.readback_targets
            if targets is None:
                targets = [self.registers.get(_addr(PLC_ACTIVE_FREQ_TARGET_START) + offset, 0) for offset in range(6)]
                channel = self.registers.get(_addr(EDGE_FREQ_CHANNEL), 0)
                if 1 <= channel <= 6:
                    targets[channel - 1] = self.registers.get(_addr(EDGE_FREQ_VALUE), 0)
            for offset, target in enumerate(targets):
                self.registers[_addr(PLC_ACTIVE_FREQ_TARGET_START) + offset] = int(target)
        return True


@pytest.mark.asyncio
async def test_frequency_transaction_writes_payload_before_sequence():
    modbus = FakeTransactionModbus()
    writer = SingleFrequencyTransactionWriter(modbus)

    result = await writer.write_one(3, 34, seq=3, timeout_sec=0.1)

    assert result.ok is True
    assert result.channel == 3
    assert result.value == 34
    assert result.active_targets == [0, 0, 34, 0, 0, 0]
    assert modbus.writes[-1] == ("single", _addr(EDGE_REQ_SEQ), 3)
    assert modbus.writes[0] == ("multi", _addr(EDGE_REQ_TYPE), [1, 2, 10, 3])
    assert modbus.writes[1] == ("single", _addr(EDGE_FREQ_VALUE), 34)
    assert modbus.writes[2] == ("multi", _addr(EDGE_REMOTE_RUN_START), [0, 0])
    assert modbus.writes[3] == (
        "single",
        _addr(EDGE_REQ_CHECK),
        sum([1, 2, 10, 3, 34, 0, 0]) & 0xFFFF,
    )


@pytest.mark.asyncio
async def test_frequency_transaction_rejects_preexisting_sequence_status():
    modbus = FakeTransactionModbus()
    modbus.registers[_addr(PLC_LAST_ACK_SEQ)] = 8
    writer = SingleFrequencyTransactionWriter(modbus)

    result = await writer.write_one(1, 31, seq=8, timeout_sec=0.1)

    assert result.ok is False
    assert result.reason == "duplicate_sequence"
    assert modbus.writes == []


@pytest.mark.asyncio
async def test_frequency_transaction_fails_closed_on_readback_mismatch():
    modbus = FakeTransactionModbus(readback_targets=[0, 0, 35, 0, 0, 0])
    writer = SingleFrequencyTransactionWriter(modbus)

    result = await writer.write_one(3, 34, seq=3, timeout_sec=0.1)

    assert result.ok is False
    assert result.reason == "readback_mismatch"
    assert result.active_targets == [0, 0, 35, 0, 0, 0]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_port(port: int, proc: asyncio.subprocess.Process, timeout_sec: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while asyncio.get_running_loop().time() < deadline:
        if proc.returncode is not None:
            stderr = b""
            if proc.stderr:
                stderr = await proc.stderr.read()
            pytest.fail(f"fake PLC exited before listening: rc={proc.returncode} stderr={stderr.decode(errors='replace')}")
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            reader.feed_eof()
            return
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(0.05)
    pytest.fail(f"fake PLC did not listen on port {port}")


async def _start_fake_plc(port: int) -> asyncio.subprocess.Process:
    workspace = Path(__file__).resolve().parents[4]
    plc_repo = workspace / "Drycontroller-plc"
    fake_plc = plc_repo / "simulator" / "fas66mt_fake_plc.py"
    if not fake_plc.exists():
        pytest.skip("Drycontroller-plc fake PLC repo is not available")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(plc_repo)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "simulator.fas66mt_fake_plc",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--interval",
        "0.05",
        "--edge-comm-timeout",
        "0.35",
        "--ramp-step",
        "50",
        "--static-process-values",
        "--static-coils",
        cwd=str(plc_repo),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await _wait_for_port(port, proc)
    return proc


async def _stop_proc(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


async def _write_raw_payload(client: ModbusClient, seq: int, channel: int, value: int) -> None:
    head = [1, 2, 10, channel]
    checksum = sum([*head, value, 0, 0]) & 0xFFFF
    assert await client.write_registers(_addr(EDGE_REQ_TYPE), head)
    assert await client.write_register(_addr(EDGE_FREQ_VALUE), value)
    assert await client.write_registers(_addr(EDGE_REMOTE_RUN_START), [0, 0])
    assert await client.write_register(_addr(EDGE_REQ_CHECK), checksum)
    assert await client.write_register(_addr(EDGE_REQ_SEQ), seq)


async def _read_until_nak(client: ModbusClient, seq: int, timeout_sec: float = 2.0) -> list[int]:
    deadline = asyncio.get_running_loop().time() + timeout_sec
    last = []
    while asyncio.get_running_loop().time() < deadline:
        registers = await client.read_registers(_addr(PLC_LAST_ACK_SEQ), 3, function_code=3)
        if registers:
            last = registers
            if len(registers) == 3 and registers[1] == seq:
                return registers
        await asyncio.sleep(0.05)
    pytest.fail(f"did not observe NAK for seq={seq}; last={last}")


@pytest.mark.asyncio
async def test_frequency_transaction_socket_e2e_against_fas66mt_fake_plc():
    port = _free_port()
    proc = await _start_fake_plc(port)
    client = ModbusClient("127.0.0.1", port=port, slave_id=1, framer="rtu")
    writer = SingleFrequencyTransactionWriter(client, poll_interval_sec=0.05)

    try:
        await writer.refresh_edge_host_alive(1)

        ok = await writer.write_one(1, 31, seq=10, timeout_sec=2.0)
        assert ok.status == "success"
        assert ok.ack_seq == 10
        assert ok.active_targets == [31, 0, 0, 0, 0, 0]

        out_of_range = await writer.write_one(3, 51, seq=11, timeout_sec=2.0)
        assert out_of_range.status == "nak"
        assert out_of_range.nak_code == 7
        assert out_of_range.active_targets == [31, 0, 0, 0, 0, 0]

        await asyncio.sleep(0.5)
        stale = await writer.write_one(2, 30, seq=12, timeout_sec=2.0)
        assert stale.status == "nak"
        assert stale.nak_code == 3

        await writer.refresh_edge_host_alive(2)
        recovered = await writer.write_one(6, 37, seq=13, timeout_sec=2.0)
        assert recovered.status == "success"
        assert recovered.active_targets == [31, 0, 0, 0, 0, 37]

        backward = await writer.write_one(1, 30, seq=9, timeout_sec=2.0)
        assert backward.status == "nak"
        assert backward.nak_code == 2

        repeated = await writer.write_one(6, 30, seq=13, timeout_sec=2.0)
        assert repeated.status == "failed"
        assert repeated.reason == "duplicate_sequence"

        await writer.refresh_edge_host_alive(3)
        await _write_raw_payload(client, 14, 1, 32)
        for value in (33, 34, 35):
            assert await client.write_register(_addr(EDGE_FREQ_VALUE), value)
            await asyncio.sleep(0.01)
        jitter = await _read_until_nak(client, 14)
        assert jitter[2] == 104

    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
        await _stop_proc(proc)
