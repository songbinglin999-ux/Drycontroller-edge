#!/usr/bin/python3
"""Disposable wide-address RTU-over-TCP fixture for the formal installer E2E."""

import asyncio
import json
import os
import struct
import sys

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer
from pymodbus.transaction import ModbusRtuFramer


CURRENT_TAGS = [
    "M1_I", "M2_I", "VFM1_I", "M4_I", "M3_I", "VFM2_I", "M6_I",
    "M5_I", "M7_I", "M8_I", "M9_I", "VFM3_I", "M11_I", "M10_I",
    "M12_I", "M13_I", "M14_I", "VF1_I", "VF2_I", "VF3_I", "M15_I",
    "M16_I",
]
TEMPERATURE_TAGS = ["FTT1_T", "FTT2_T", "FTT3_T", "TT1_T", "TT2_T"]
LIMIT_TAGS = ["LL1", "LL2", "LL3", "LL4", "LL5", "LL6"]
AUTHORITY_TAGS = [
    "AUTHORITY_OWNER", "AUTHORITY_EPOCH", "AUTH_TRANSITION_STATE", "HMI_DIRECT_ENABLE",
]
TARGET_TAGS = [
    "VFM1_ACTIVE_FREQ_TARGET", "VFM2_ACTIVE_FREQ_TARGET", "VFM3_ACTIVE_FREQ_TARGET",
    "VF1_ACTIVE_FREQ_TARGET", "VF2_ACTIVE_FREQ_TARGET", "VF3_ACTIVE_FREQ_TARGET",
]


def expected_tags() -> dict[str, dict[str, object]]:
    expected: dict[str, dict[str, object]] = {}
    for index, name in enumerate(CURRENT_TAGS):
        expected[name] = {"value": (101 + index) / 10, "unit": "A", "quality": "good"}
    for index, name in enumerate(TEMPERATURE_TAGS):
        expected[name] = {"value": (401 + index) / 10, "unit": "℃", "quality": "good"}
    expected["MT1_H"] = {"value": 32, "unit": "%", "quality": "good"}
    for name, value in zip(LIMIT_TAGS, (True, False, True, False, True, False), strict=True):
        expected[name] = {"value": value, "unit": "", "quality": "good"}
    expected["PLC_LEGACY_HEARTBEAT_TOGGLE"] = {
        "allowed_values": [False, True],
        "unit": "",
        "quality": "good",
        "must_transition": True,
    }
    for name, value in zip(AUTHORITY_TAGS, (2, 17, 0, 0), strict=True):
        expected[name] = {"value": value, "unit": "", "quality": "good"}
    for name, value in zip(TARGET_TAGS, (11, 12, 13, 14, 15, 16), strict=True):
        expected[name] = {"value": value, "unit": "Hz", "quality": "good"}
    if len(expected) != 45:
        raise RuntimeError("formal Modbus expectation inventory is not 45")
    return expected


def _int32_words(value: int) -> list[int]:
    return list(struct.unpack("<HH", struct.pack("<i", value)))


def seed_fixture(context: ModbusServerContext, slave_id: int) -> None:
    slave = context[slave_id]
    slave.setValues(3, 40000, list(range(101, 123)))
    slave.setValues(3, 40040, list(range(401, 406)))
    slave.setValues(3, 40045, _int32_words(32))
    for address, value in zip((0, 1, 2, 5, 3, 6), (1, 0, 1, 0, 1, 0), strict=True):
        slave.setValues(1, address, [value])
    slave.setValues(3, 45201, [2])
    slave.setValues(3, 45414, [17])
    slave.setValues(3, 45415, [0])
    slave.setValues(3, 45571, [0])
    slave.setValues(3, 45210, [11, 12, 13, 14, 15, 16])


async def toggle_heartbeat(context: ModbusServerContext, slave_id: int) -> None:
    value = 0
    while True:
        value ^= 1
        context[slave_id].setValues(3, 45290, [value])
        await asyncio.sleep(0.5)


async def toggle_legacy_heartbeat(context: ModbusServerContext, slave_id: int) -> None:
    value = 0
    while True:
        value ^= 1
        context[slave_id].setValues(1, 31001, [value])
        await asyncio.sleep(0.4)


async def main() -> None:
    slave_id = int(os.environ.get("MODBUS_SIM_SLAVE", "1"))
    blocks = {
        name: ModbusSequentialDataBlock(0, [0] * 65536)
        for name in ("di", "co", "hr", "ir")
    }
    context = ModbusServerContext(
        slaves={slave_id: ModbusSlaveContext(**blocks, zero_mode=True)},
        single=False,
    )
    seed_fixture(context, slave_id)
    asyncio.create_task(toggle_heartbeat(context, slave_id))
    asyncio.create_task(toggle_legacy_heartbeat(context, slave_id))
    await StartAsyncTcpServer(
        context=context,
        address=(os.environ["MODBUS_SIM_HOST"], int(os.environ.get("MODBUS_SIM_PORT", "2022"))),
        framer=ModbusRtuFramer,
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["--print-expectations"]:
        print(json.dumps(expected_tags(), sort_keys=True, separators=(",", ":")))
    else:
        asyncio.run(main())
