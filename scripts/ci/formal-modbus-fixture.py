#!/usr/bin/python3
"""Disposable wide-address RTU-over-TCP fixture for the formal installer E2E."""

import asyncio
import os

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer
from pymodbus.transaction import ModbusRtuFramer


async def toggle_heartbeat(context: ModbusServerContext, slave_id: int) -> None:
    value = 0
    while True:
        value ^= 1
        context[slave_id].setValues(3, 45290, [value])
        await asyncio.sleep(1)


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
    asyncio.create_task(toggle_heartbeat(context, slave_id))
    await StartAsyncTcpServer(
        context=context,
        address=(os.environ["MODBUS_SIM_HOST"], int(os.environ.get("MODBUS_SIM_PORT", "2022"))),
        framer=ModbusRtuFramer,
    )


if __name__ == "__main__":
    asyncio.run(main())
