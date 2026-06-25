"""
Modbus RTU-over-TCP 从站模拟器（联调用，非生产服务）。

目的：没有真实 PLC 时，让 edge-daq 走真实 Modbus 链路（RTU 帧 / TCP 传输 /
端口 2022 / 站号 1）读到随时间变化的点位，验证「PLC 通讯 + 采集」e2e。
切真实 PLC 时只需把 PLC_HOST 指到真机（MODBUS_FRAMER=rtu / PLC_PORT 保持），
edge 侧代码路径不变。

点位来自集成商真实点表「数据点位表.csv」的 DryController 当前「读」点（D 字寄存器，0x03）：
  电流 D0-D21 / 温度湿度 D40-D45 / 湿度状态 D53。
D30-D35 属于 PLC 原工程/PLC 屏侧旧频率链路，不进入 DryController 采集或控制路径。
Modbus 地址 = D_BASE + D 序号（PLC 偏移表：D0=40000）。

包含当前 6 个 X 料位 / 限位线圈（0x01 读）：X000/X001/X002/X005/X003/X006。
M 报警 / Y 输出仍未纳入本期采集，需现场对账后再决策。

注意：D 是 16-bit 字，这里发出的是随时间变化的「原始值」。集成商确认的工程量换算：
电流原始值×10（scale 0.1，A）、温度原始值×10（scale 0.1，℃）；
湿度换算 TBD。scale 写在 modbus-address-map.sim.yaml，本模拟器只发原始值。
真机上 D0 究竟对应原始地址 40000 还是 4x 参考号（差 40001）需现场确认；模拟器
client/server 两端地址一致，不受该歧义影响。

运行：
    MODBUS_SIM_PORT=2022 .venv-test/bin/python services/edge-daq/modbus_slave_sim.py
"""
import asyncio
import math
import os
import struct
import time

from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext, ModbusSparseDataBlock
from pymodbus.server import StartAsyncTcpServer
from pymodbus.transaction import ModbusRtuFramer

D_BASE = 40000

# 单字（无符号 16-bit）读点：集成商确认电流/温度无符号。
CURRENTS = list(range(0, 22))   # D0-D21 各电机电流
TEMPS = list(range(40, 45))     # D40-D44 温度
HUMIDITY_STATUS = 53            # D53 湿度通讯/状态字
WORD_D = CURRENTS + TEMPS + [HUMIDITY_STATUS]
# 双字（32-bit 带符号 int32）读点：湿度 D45 = 40045+40046（集成商计划用带符号 32 位）。
HUMIDITY = 45
ALL_D = WORD_D + [HUMIDITY]
# 料位/限位线圈（0x01）：X000/X001/X002/X003/X005/X006 → 线圈地址 0/1/2/3/5/6。
COIL_ADDRS = [0, 1, 2, 3, 5, 6]


def _value(dnum: int, t: float) -> int:
    """按点位类别给出随时间变化的合理原始值（非工程量）。"""
    if dnum in CURRENTS:
        return int(max(0, 100 + (dnum * 7) % 80 + 15 * math.sin(t / 5.0 + dnum)))   # ×0.1=A → ~10-18A
    if dnum in TEMPS:
        return int(max(0, 650 + 80 * math.sin(t / 20.0 + dnum)))   # ×0.1=℃ → ~57-73℃
    if dnum == HUMIDITY:
        return int(250 + 60 * math.sin(t / 15.0))                  # int32，scale TBD
    if dnum == HUMIDITY_STATUS:
        return 0
    return 0


def _int32_words(value: int) -> list[int]:
    """带符号 32-bit → [低字, 高字]（低字在前 / little word order，集成商确认；
    与 plc_reader read_int32 byte_order=little 一致）。"""
    lo, hi = struct.unpack("<HH", struct.pack("<i", int(value)))
    return [lo, hi]


def _initial_values() -> dict[int, int]:
    values = {D_BASE + n: 0 for n in WORD_D}
    values[D_BASE + HUMIDITY] = 0
    values[D_BASE + HUMIDITY + 1] = 0   # int32 第二个寄存器
    return values


async def _updater(context: ModbusServerContext, slave_id: int, interval: float):
    t0 = time.time()
    while True:
        t = time.time() - t0
        slave = context[slave_id]
        for n in WORD_D:
            slave.setValues(3, D_BASE + n, [_value(n, t)])
        slave.setValues(3, D_BASE + HUMIDITY, _int32_words(_value(HUMIDITY, t)))
        # 线圈：各料位/限位按不同周期 0/1 翻转（模拟满/未满到位状态）
        for i, c in enumerate(COIL_ADDRS):
            slave.setValues(1, c, [1 if int(t / 4 + i) % 2 == 0 else 0])
        await asyncio.sleep(interval)


async def main():
    host = os.getenv("MODBUS_SIM_HOST", "0.0.0.0")
    port = int(os.getenv("MODBUS_SIM_PORT", "2022"))
    slave_id = int(os.getenv("MODBUS_SIM_SLAVE", "1"))
    interval = float(os.getenv("MODBUS_SIM_INTERVAL", "1.0"))

    hr = ModbusSparseDataBlock(_initial_values())
    co = ModbusSparseDataBlock({c: 0 for c in COIL_ADDRS})
    slave_ctx = ModbusSlaveContext(co=co, hr=hr, zero_mode=True)
    context = ModbusServerContext(slaves={slave_id: slave_ctx}, single=False)

    asyncio.create_task(_updater(context, slave_id, interval))
    print(
        f"[modbus-sim] RTU-over-TCP slave on {host}:{port} slave_id={slave_id} "
        f"D-points={len(ALL_D)} (currents={len(CURRENTS)} "
        f"temp={len(TEMPS)} humidity=1[int32] humidity_status=1) coils={len(COIL_ADDRS)}[料位/限位]",
        flush=True,
    )
    await StartAsyncTcpServer(context=context, address=(host, port), framer=ModbusRtuFramer)


if __name__ == "__main__":
    asyncio.run(main())
