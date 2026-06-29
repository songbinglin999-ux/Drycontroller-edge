import asyncio
import contextlib
import os
from pathlib import Path
import socket
import sys
from types import ModuleType

import pytest

from daq.frequency_transaction import (
    AUTH_ACTION_REQUEST_OWNER,
    AUTH_GRANT_SEQ,
    AUTH_REJECT_CODE,
    AUTH_REJECT_SEQ,
    AUTH_REQ_ACTION,
    AUTH_REQ_EPOCH,
    AUTH_REQ_SEQ,
    AUTH_REQ_SOURCE,
    AUTH_SOURCE_EDGE,
    AUTH_TRANSITION_STATE,
    AUTHORITY_EPOCH,
    AUTHORITY_OWNER,
    AUTHORITY_OWNER_EDGE,
    AUTHORITY_OWNER_HMI_DIRECT,
    AUTHORITY_OWNER_LOCAL_SCREEN,
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
    def __init__(self, *, readback_targets=None, authority_owner=AUTHORITY_OWNER_EDGE):
        self.registers = {
            _addr(AUTHORITY_OWNER): int(authority_owner),
            _addr(AUTHORITY_EPOCH): 1,
            _addr(AUTH_TRANSITION_STATE): 0,
        }
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
        if address == _addr(AUTH_REQ_SEQ):
            self.registers[_addr(AUTH_REQ_SEQ)] = int(value)
            self.registers[_addr(AUTH_GRANT_SEQ)] = 0
            self.registers[_addr(AUTH_REJECT_SEQ)] = 0
            self.registers[_addr(AUTH_REJECT_CODE)] = 0
            if (
                self.registers.get(_addr(AUTH_REQ_SOURCE)) == AUTH_SOURCE_EDGE
                and self.registers.get(_addr(AUTH_REQ_ACTION)) == AUTH_ACTION_REQUEST_OWNER
                and self.registers.get(_addr(AUTH_REQ_EPOCH)) == self.registers.get(_addr(AUTHORITY_EPOCH))
                and self.registers.get(_addr(AUTHORITY_OWNER)) in {AUTHORITY_OWNER_EDGE, AUTHORITY_OWNER_LOCAL_SCREEN}
            ):
                self.registers[_addr(AUTH_GRANT_SEQ)] = int(value)
                self.registers[_addr(AUTHORITY_OWNER)] = AUTHORITY_OWNER_EDGE
                self.registers[_addr(AUTHORITY_EPOCH)] = self.registers[_addr(AUTHORITY_EPOCH)] + 1
            else:
                self.registers[_addr(AUTH_REJECT_SEQ)] = int(value)
                self.registers[_addr(AUTH_REJECT_CODE)] = 302
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
async def test_frequency_transaction_fails_closed_when_local_screen_owns_authority():
    modbus = FakeTransactionModbus(authority_owner=AUTHORITY_OWNER_LOCAL_SCREEN)
    writer = SingleFrequencyTransactionWriter(modbus)

    result = await writer.write_one(1, 31, seq=4, timeout_sec=0.1)

    assert result.ok is False
    assert result.reason == "authority_owner_not_edge"
    assert result.detail["authority_owner"] == AUTHORITY_OWNER_LOCAL_SCREEN
    assert modbus.writes == []


@pytest.mark.asyncio
async def test_frequency_transaction_explicit_reauthorization_can_request_edge_authority():
    modbus = FakeTransactionModbus(authority_owner=AUTHORITY_OWNER_LOCAL_SCREEN)
    writer = SingleFrequencyTransactionWriter(modbus)

    result = await writer.write_one(1, 31, seq=4, timeout_sec=0.1, allow_authority_request=True)

    assert result.ok is True
    assert modbus.writes[:2] == [
        ("multi", _addr(AUTH_REQ_SOURCE), [AUTH_SOURCE_EDGE, AUTH_ACTION_REQUEST_OWNER, 1, 10]),
        ("single", _addr(AUTH_REQ_SEQ), 1),
    ]
    assert modbus.registers[_addr(AUTHORITY_OWNER)] == AUTHORITY_OWNER_EDGE
    assert modbus.registers[_addr(AUTHORITY_EPOCH)] == 2
    assert ("single", _addr(EDGE_REQ_SEQ), 4) in modbus.writes


@pytest.mark.asyncio
async def test_frequency_transaction_fails_closed_when_hmi_direct_owns_authority():
    modbus = FakeTransactionModbus(authority_owner=AUTHORITY_OWNER_HMI_DIRECT)
    writer = SingleFrequencyTransactionWriter(modbus)

    result = await writer.write_one(1, 31, seq=4, timeout_sec=0.1)

    assert result.ok is False
    assert result.reason == "authority_owner_not_edge"
    assert result.detail["authority_owner"] == AUTHORITY_OWNER_HMI_DIRECT
    assert all(write[1] != _addr(EDGE_REQ_SEQ) for write in modbus.writes)
    assert all(write[1] != _addr(AUTH_REQ_SEQ) for write in modbus.writes)


@pytest.mark.asyncio
async def test_frequency_transaction_fails_closed_when_authority_epoch_is_uninitialized():
    modbus = FakeTransactionModbus(authority_owner=AUTHORITY_OWNER_EDGE)
    modbus.registers[_addr(AUTHORITY_EPOCH)] = 0
    writer = SingleFrequencyTransactionWriter(modbus)

    result = await writer.write_one(1, 31, seq=4, timeout_sec=0.1)

    assert result.ok is False
    assert result.reason == "authority_epoch_invalid"
    assert all(write[1] != _addr(EDGE_REQ_SEQ) for write in modbus.writes)
    assert all(write[1] != _addr(AUTH_REQ_SEQ) for write in modbus.writes)


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
@pytest.mark.parametrize("bad_seq", [True, 1.9, "1.9"])
async def test_frequency_transaction_rejects_invalid_sequence_without_write(bad_seq):
    modbus = FakeTransactionModbus()
    writer = SingleFrequencyTransactionWriter(modbus)

    result = await writer.write_one(1, 31, seq=bad_seq, timeout_sec=0.1)

    assert result.ok is False
    assert result.reason == "invalid_sequence"
    assert modbus.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"channel": 1.9, "value": 31, "mode": 2},
        {"channel": "1.9", "value": 31, "mode": 2},
        {"channel": 1, "value": 31, "mode": 1.9},
        {"channel": 1, "value": 31, "mode": "1.9"},
    ],
)
async def test_frequency_transaction_rejects_fractional_integer_fields_without_write(kwargs):
    modbus = FakeTransactionModbus()
    writer = SingleFrequencyTransactionWriter(modbus)

    result = await writer.write_one(**kwargs, seq=3, timeout_sec=0.1)

    assert result.ok is False
    assert result.reason == "invalid_request"
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


def _load_plc_core() -> ModuleType:
    workspace = Path(__file__).resolve().parents[4]
    plc_repo = workspace / "Drycontroller-plc"
    if not (plc_repo / "simulator" / "fas66mt_core.py").exists():
        pytest.skip("Drycontroller-plc simulator core is not available")
    if str(plc_repo) not in sys.path:
        sys.path.insert(0, str(plc_repo))

    from simulator import fas66mt_core as core

    return core


class CoreSimulatorModbus:
    def __init__(self, simulator, *, scan_seconds: float = 0.05):
        self.simulator = simulator
        self.scan_seconds = scan_seconds

    def _d_number(self, address: int) -> int:
        return int(address) - D_MODBUS_BASE

    async def read_registers(self, address: int, count: int = 1, function_code: int = 3, retries: int = 2):
        self.simulator.advance(self.scan_seconds)
        return self.simulator.read_holding(self._d_number(address), count)

    async def write_registers(self, address: int, values: list[int], retries: int = 2):
        self.simulator.write_holdings(self._d_number(address), values)
        self.simulator.advance(self.scan_seconds)
        return True

    async def write_register(self, address: int, value: int, retries: int = 2):
        self.simulator.write_holding(self._d_number(address), value)
        self.simulator.advance(self.scan_seconds)
        return True


def _make_core_simulator(core: ModuleType):
    return core.Fas66mtSimulator(
        core.SimulatorConfig(
            dynamic_process_values=False,
            dynamic_coils=False,
            edge_comm_timeout_s=2.0,
            edge_control_timeout_s=1.0,
        )
    )


def _write_auth_request(core: ModuleType, simulator, *, seq: int, source: int, action: int, epoch: int) -> None:
    simulator.write_holding(core.AUTH_REQ_SOURCE, source)
    simulator.write_holding(core.AUTH_REQ_ACTION, action)
    simulator.write_holding(core.AUTH_REQ_EPOCH, epoch)
    simulator.write_holding(core.AUTH_REQ_SEQ, seq)
    simulator.advance(0.1)


def _write_local_screen_request(core: ModuleType, simulator, *, seq: int, epoch: int, values: list[int]) -> None:
    simulator.write_holding(core.LOCAL_SCREEN_ACTION, core.LocalScreenAction.WRITE_FREQ_TARGETS)
    simulator.write_holding(core.LOCAL_SCREEN_EPOCH, epoch)
    simulator.write_holding(core.LOCAL_SCREEN_MODE, 1)
    simulator.write_holdings(core.LOCAL_SCREEN_FREQ_SP_D[0], values)
    simulator.write_holding(core.LOCAL_SCREEN_REQ_SEQ, seq)
    simulator.advance(0.1)
    simulator.advance(0.1)


def _write_hmi_direct_request(core: ModuleType, simulator, *, seq: int, epoch: int, values: list[int]) -> None:
    simulator.write_holding(core.HMI_DIRECT_ACTION, core.HmiDirectAction.WRITE_FREQ_TARGETS)
    simulator.write_holding(core.HMI_DIRECT_EPOCH, epoch)
    simulator.write_holding(core.HMI_DIRECT_MODE, 1)
    simulator.write_holdings(core.HMI_DIRECT_FREQ_SP_D[0], values)
    simulator.write_holding(core.HMI_DIRECT_REQ_SEQ, seq)
    simulator.advance(0.1)
    simulator.advance(0.1)


@pytest.mark.asyncio
async def test_frequency_transaction_mode_handoff_matrix_against_fas66mt_core():
    core = _load_plc_core()

    simulator = _make_core_simulator(core)
    writer = SingleFrequencyTransactionWriter(CoreSimulatorModbus(simulator), poll_interval_sec=0.01)

    await writer.refresh_edge_host_alive(1)
    auto = await writer.write_one(1, 31, seq=10, mode=2, timeout_sec=1.0, allow_authority_request=True)
    assert auto.status == "success"
    assert simulator.read_holding(core.AUTHORITY_OWNER)[0] == core.AuthorityOwner.EDGE
    assert simulator.read_holding(core.PLC_CONTROL_STATE)[0] == core.ControlState.EDGE_ACTIVE
    assert simulator.read_holding(core.PLC_ACTIVE_FREQ_TARGET_D[0], 6) == [31, 0, 0, 0, 0, 0]

    manual = await writer.write_one(2, 32, seq=11, mode=1, timeout_sec=1.0)
    assert manual.status == "success"
    assert manual.active_targets == [31, 32, 0, 0, 0, 0]

    edge_epoch = simulator.read_holding(core.AUTHORITY_EPOCH)[0]
    _write_auth_request(
        core,
        simulator,
        seq=20,
        source=core.AuthRequestSource.LOCAL_SCREEN,
        action=core.AuthRequestAction.REQUEST_OWNER,
        epoch=edge_epoch,
    )
    local_epoch = simulator.read_holding(core.AUTHORITY_EPOCH)[0]
    _write_local_screen_request(core, simulator, seq=21, epoch=local_epoch, values=[11, 12, 13, 14, 15, 16])

    assert simulator.read_holding(core.AUTHORITY_OWNER)[0] == core.AuthorityOwner.LOCAL_SCREEN
    assert simulator.read_holding(core.ACTIVE_FREQ_SOURCE)[0] == core.FreqSource.LOCAL_SCREEN
    assert simulator.read_holding(core.PLC_CONTROL_STATE)[0] == core.ControlState.LOCAL_MANUAL
    assert simulator.read_holding(core.LOCAL_SCREEN_ACK_SEQ)[0] == 21
    assert simulator.read_holding(core.PLC_ACTIVE_FREQ_TARGET_D[0], 6) == [11, 12, 13, 14, 15, 16]

    blocked_by_local = await writer.write_one(3, 33, seq=12, mode=2, timeout_sec=1.0)
    assert blocked_by_local.status == "failed"
    assert blocked_by_local.reason == "authority_owner_not_edge"
    assert blocked_by_local.detail["authority_owner"] == AUTHORITY_OWNER_LOCAL_SCREEN

    hmi_simulator = _make_core_simulator(core)
    hmi_writer = SingleFrequencyTransactionWriter(CoreSimulatorModbus(hmi_simulator), poll_interval_sec=0.01)
    await hmi_writer.refresh_edge_host_alive(1)
    initial = await hmi_writer.write_one(1, 31, seq=30, mode=2, timeout_sec=1.0, allow_authority_request=True)
    assert initial.status == "success"
    edge_epoch = hmi_simulator.read_holding(core.AUTHORITY_EPOCH)[0]
    hmi_simulator.advance(2.5)
    assert hmi_simulator.read_holding(core.PLC_CONTROL_STATE)[0] == core.ControlState.AUTO_SUSPENDED
    assert hmi_simulator.read_holding(core.PLC_SUSPEND_REASON)[0] == 103

    _write_auth_request(
        core,
        hmi_simulator,
        seq=31,
        source=core.AuthRequestSource.HMI_DIRECT,
        action=core.AuthRequestAction.REQUEST_HMI_DIRECT,
        epoch=edge_epoch,
    )
    hmi_epoch = hmi_simulator.read_holding(core.AUTHORITY_EPOCH)[0]
    _write_hmi_direct_request(core, hmi_simulator, seq=32, epoch=hmi_epoch, values=[21, 22, 23, 24, 25, 26])

    assert hmi_simulator.read_holding(core.AUTHORITY_OWNER)[0] == core.AuthorityOwner.HMI_DIRECT
    assert hmi_simulator.read_holding(core.HMI_DIRECT_ENABLE)[0] == 1
    assert hmi_simulator.read_holding(core.HMI_DIRECT_ACK_SEQ)[0] == 32
    assert hmi_simulator.read_holding(core.ACTIVE_FREQ_SOURCE)[0] == core.FreqSource.HMI_DIRECT
    assert hmi_simulator.read_holding(core.PLC_ACTIVE_FREQ_TARGET_D[0], 6) == [21, 22, 23, 24, 25, 26]

    restore_simulator = _make_core_simulator(core)
    restore_writer = SingleFrequencyTransactionWriter(CoreSimulatorModbus(restore_simulator), poll_interval_sec=0.01)
    await restore_writer.refresh_edge_host_alive(1)
    first = await restore_writer.write_one(1, 31, seq=40, mode=2, timeout_sec=1.0, allow_authority_request=True)
    assert first.status == "success"
    restore_simulator.advance(2.5)
    assert restore_simulator.read_holding(core.PLC_CONTROL_STATE)[0] == core.ControlState.AUTO_SUSPENDED

    await restore_writer.refresh_edge_host_alive(2)
    no_auto_restore = await restore_writer.write_one(2, 32, seq=41, mode=2, timeout_sec=1.0)
    assert no_auto_restore.status == "nak"
    assert no_auto_restore.nak_code == 5
    assert restore_simulator.read_holding(core.PLC_CONTROL_STATE)[0] == core.ControlState.AUTO_SUSPENDED
    assert restore_simulator.read_holding(core.PLC_ACTIVE_FREQ_TARGET_D[0], 6) == [31, 0, 0, 0, 0, 0]


@pytest.mark.asyncio
async def test_frequency_transaction_socket_e2e_against_fas66mt_fake_plc():
    port = _free_port()
    proc = await _start_fake_plc(port)
    client = ModbusClient("127.0.0.1", port=port, slave_id=1, framer="rtu")
    writer = SingleFrequencyTransactionWriter(client, poll_interval_sec=0.05)

    try:
        await writer.refresh_edge_host_alive(1)

        ok = await writer.write_one(1, 31, seq=10, timeout_sec=2.0, allow_authority_request=True)
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
        assert stale.reason == "plc_nak_3"
        assert stale.nak_code == 3
        assert stale.active_targets == [31, 0, 0, 0, 0, 0]

        await writer.refresh_edge_host_alive(2)
        recovered_without_reauth = await writer.write_one(6, 37, seq=13, timeout_sec=2.0)
        assert recovered_without_reauth.status == "nak"
        assert recovered_without_reauth.reason == "plc_nak_5"
        assert recovered_without_reauth.nak_code == 5
        assert recovered_without_reauth.active_targets == [31, 0, 0, 0, 0, 0]

        recovered = await writer.write_one(6, 37, seq=14, timeout_sec=2.0, allow_authority_request=True)
        assert recovered.status == "success"
        assert recovered.active_targets == [31, 0, 0, 0, 0, 37]

        backward = await writer.write_one(1, 30, seq=9, timeout_sec=2.0)
        assert backward.status == "nak"
        assert backward.nak_code == 2

        repeated = await writer.write_one(6, 30, seq=14, timeout_sec=2.0)
        assert repeated.status == "failed"
        assert repeated.reason == "duplicate_sequence"

        await writer.refresh_edge_host_alive(3)
        await _write_raw_payload(client, 15, 1, 32)
        for value in (33, 34, 35):
            assert await client.write_register(_addr(EDGE_FREQ_VALUE), value)
            await asyncio.sleep(0.01)
        jitter = await _read_until_nak(client, 15)
        assert jitter[2] == 104

    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
        await _stop_proc(proc)
