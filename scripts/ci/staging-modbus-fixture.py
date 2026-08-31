#!/usr/bin/python3
"""Disposable RTU-over-TCP fixture for Edge staging acceptance only."""

from __future__ import annotations

import asyncio
import math
import os
import struct
import time

from pymodbus.datastore import (
    ModbusServerContext,
    ModbusSlaveContext,
    ModbusSparseDataBlock,
)
from pymodbus.server import StartAsyncTcpServer
from pymodbus.transaction import ModbusRtuFramer


CURRENT_ADDRESSES = list(range(40000, 40022))
TEMPERATURE_ADDRESSES = list(range(40040, 40045))
HUMIDITY_ADDRESS = 40045
HUMIDITY_STATUS_ADDRESS = 40053
PLC_HEARTBEAT_ADDRESS = 45290
EDGE_HOST_ALIVE_ADDRESS = 45291
AUTHORITY_ADDRESSES = [45201, 45414, 45415, 45571]
ACTIVE_TARGET_ADDRESSES = list(range(45210, 45216))
COIL_ADDRESSES = [0, 1, 2, 3, 5, 6, 31001]


def int32_words(value: int) -> list[int]:
    low, high = struct.unpack("<HH", struct.pack("<i", int(value)))
    return [low, high]


def initial_holding() -> dict[int, int]:
    values = {address: 1 for address in CURRENT_ADDRESSES + TEMPERATURE_ADDRESSES}
    values.update(
        {HUMIDITY_ADDRESS: 0, HUMIDITY_ADDRESS + 1: 0, HUMIDITY_STATUS_ADDRESS: 0}
    )
    values.update({PLC_HEARTBEAT_ADDRESS: 0, EDGE_HOST_ALIVE_ADDRESS: 0})
    values.update({45201: 2, 45414: 7, 45415: 0, 45571: 0})
    values.update(
        {
            address: 20 + index
            for index, address in enumerate(ACTIVE_TARGET_ADDRESSES, start=1)
        }
    )
    return values


async def update(context: ModbusServerContext, slave_id: int) -> None:
    started = time.monotonic()
    heartbeat = 0
    while True:
        elapsed = time.monotonic() - started
        heartbeat = (heartbeat + 1) & 0xFFFF
        slave = context[slave_id]
        for index, address in enumerate(CURRENT_ADDRESSES):
            slave.setValues(
                3, address, [120 + index * 3 + int(8 * math.sin(elapsed + index))]
            )
        for index, address in enumerate(TEMPERATURE_ADDRESSES):
            slave.setValues(
                3, address, [650 + index * 20 + int(15 * math.sin(elapsed / 3 + index))]
            )
        slave.setValues(
            3, HUMIDITY_ADDRESS, int32_words(260 + int(12 * math.sin(elapsed / 2)))
        )
        slave.setValues(3, HUMIDITY_STATUS_ADDRESS, [1])
        slave.setValues(3, PLC_HEARTBEAT_ADDRESS, [heartbeat])
        for index, address in enumerate(ACTIVE_TARGET_ADDRESSES):
            slave.setValues(3, address, [21 + index])
        for index, address in enumerate(COIL_ADDRESSES):
            slave.setValues(1, address, [bool((int(elapsed) + index) % 2)])
        await asyncio.sleep(0.2)


async def main() -> None:
    host = os.getenv("MODBUS_SIM_HOST", "0.0.0.0")
    port = int(os.getenv("MODBUS_SIM_PORT", "2022"))
    slave_id = int(os.getenv("MODBUS_SIM_SLAVE", "1"))
    holding = ModbusSparseDataBlock(initial_holding())
    coils = ModbusSparseDataBlock({address: False for address in COIL_ADDRESSES})
    slave = ModbusSlaveContext(co=coils, hr=holding, zero_mode=True)
    context = ModbusServerContext(slaves={slave_id: slave}, single=False)
    asyncio.create_task(update(context, slave_id))
    print(
        f"staging modbus fixture listening on {host}:{port} slave={slave_id}",
        flush=True,
    )
    await StartAsyncTcpServer(
        context=context, address=(host, port), framer=ModbusRtuFramer
    )


if __name__ == "__main__":
    asyncio.run(main())
