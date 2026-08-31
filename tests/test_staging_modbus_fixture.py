from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scripts/ci/staging-modbus-fixture.py"


def _load_fixture():
    spec = importlib.util.spec_from_file_location("staging_modbus_fixture", FIXTURE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _StopFixture(Exception):
    pass


class _RecordingSlave:
    def __init__(self) -> None:
        self.writes: list[tuple[int, int, list[int | bool]]] = []

    def setValues(
        self, function_code: int, address: int, values: list[int | bool]
    ) -> None:
        self.writes.append((function_code, address, values))


class _Context:
    def __init__(self, slave: _RecordingSlave) -> None:
        self.slave = slave

    def __getitem__(self, slave_id: int) -> _RecordingSlave:
        assert slave_id == 1
        return self.slave


def test_staging_plc_heartbeat_increments_every_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _load_fixture()
    slave = _RecordingSlave()
    sleeps = 0

    async def stop_after_three_updates(delay: float) -> None:
        nonlocal sleeps
        assert delay == 0.2
        sleeps += 1
        if sleeps == 3:
            raise _StopFixture

    monkeypatch.setattr(fixture.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(fixture.asyncio, "sleep", stop_after_three_updates)

    with pytest.raises(_StopFixture):
        asyncio.run(fixture.update(_Context(slave), 1))

    heartbeat_values = [
        values[0]
        for function_code, address, values in slave.writes
        if function_code == 3 and address == fixture.PLC_HEARTBEAT_ADDRESS
    ]
    assert heartbeat_values == [1, 2, 3]


def test_staging_fixture_preserves_existing_plc_contract_addresses() -> None:
    fixture = _load_fixture()
    holding = fixture.initial_holding()

    assert holding[fixture.PLC_HEARTBEAT_ADDRESS] == 0
    assert holding[fixture.EDGE_HOST_ALIVE_ADDRESS] == 0
    assert set(fixture.AUTHORITY_ADDRESSES).issubset(holding)
    assert set(fixture.ACTIVE_TARGET_ADDRESSES).issubset(holding)
    assert fixture.COIL_ADDRESSES == [0, 1, 2, 3, 5, 6, 31001]
