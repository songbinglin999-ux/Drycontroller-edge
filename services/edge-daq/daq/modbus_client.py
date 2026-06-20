"""
Modbus TCP 客户端
"""

import asyncio
import time
import os
import structlog
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
from pymodbus.transaction import ModbusRtuFramer
from prometheus_client import Counter, Gauge
from prometheus_client.registry import REGISTRY

logger = structlog.get_logger(__name__)

MODBUS_READ_SUCCESS = Counter("modbus_read_success_total", "Modbus read success", ["function_code"])
MODBUS_READ_FAILURE = Counter("modbus_read_failure_total", "Modbus read failure", ["function_code"])
MODBUS_WRITE_SUCCESS = Counter("modbus_write_success_total", "Modbus write success", ["op"])
MODBUS_WRITE_FAILURE = Counter("modbus_write_failure_total", "Modbus write failure", ["op"])
MODBUS_CONNECTED = Gauge("modbus_connected", "Modbus connection state")
MODBUS_LAST_LATENCY_MS = Gauge("modbus_last_latency_ms", "Last modbus op latency in ms", ["op"])
if "plc_heartbeat_missing_total" in REGISTRY._names_to_collectors:
    PLC_HEARTBEAT_MISSING = REGISTRY._names_to_collectors["plc_heartbeat_missing_total"]
else:
    PLC_HEARTBEAT_MISSING = Counter("plc_heartbeat_missing_total", "PLC heartbeat missing detected")
MODBUS_CIRCUIT_OPEN = Counter("modbus_circuit_open_total", "Modbus circuit open events")


class ModbusClient:
    """Modbus TCP 客户端封装，带重试和重连"""

    def __init__(self, host: str, port: int = 502, slave_id: int = 1, connect_timeout: int = 5, framer=None):
        self.host = host
        self.port = port
        self.slave_id = slave_id
        # framer：RTU-over-TCP（CRC 帧）vs 标准 Modbus TCP（MBAP）。
        # 此 PLC 走 RTU-over-TCP，需 MODBUS_FRAMER=rtu；未设置时维持默认 socket framer。
        self.framer = self._resolve_framer(framer if framer is not None else os.getenv("MODBUS_FRAMER", ""))
        self.client = None
        self.connected = False
        self.connect_timeout = connect_timeout
        self.last_error = None
        self.last_success_ts = None
        self.failure_count = 0
        self.reconnect_count = 0
        # 简单熔断器
        self.cb_fail_threshold = int(os.getenv("MODBUS_CB_FAIL_THRESHOLD", "5") or "5")
        self.cb_cooldown = float(os.getenv("MODBUS_CB_COOLDOWN", "5") or "5")
        self.cb_open_until = 0.0

    @staticmethod
    def _resolve_framer(name):
        """env/参数 → framer 类；rtu 系列用 ModbusRtuFramer，其余返回 None（pymodbus 默认 socket framer）。"""
        if (name or "").strip().lower() in ("rtu", "rtu-over-tcp", "rtutcp"):
            return ModbusRtuFramer
        return None

    async def connect(self):
        """连接到 Modbus 设备"""
        try:
            kwargs = dict(host=self.host, port=self.port, timeout=self.connect_timeout)
            if self.framer is not None:
                kwargs["framer"] = self.framer
            self.client = AsyncModbusTcpClient(**kwargs)

            await self.client.connect()
            self.connected = self.client.connected

            if self.connected:
                logger.info(
                    "modbus_connected",
                    host=self.host,
                    port=self.port,
                    slave_id=self.slave_id,
                )
                self.reconnect_count += 1
                self.last_error = None
                MODBUS_CONNECTED.set(1)
            else:
                logger.error("modbus_connection_failed")
                self._update_failure("connection_failed")

        except (ModbusException, OSError, RuntimeError, TypeError, ValueError) as e:
            logger.error("modbus_connect_error", error=str(e))
            self.connected = False
            self.last_error = str(e)
            self.failure_count += 1

    async def disconnect(self):
        """断开连接"""
        if self.client:
            self.client.close()
            self.connected = False
            logger.info("modbus_disconnected")
            MODBUS_CONNECTED.set(0)

    async def ensure_connected(self):
        """确保连接可用，不可用则尝试重连"""
        now = time.time()
        if self.cb_open_until > now:
            logger.warning("modbus_circuit_open", open_until=self.cb_open_until)
            return False
        if self.connected and self.client and self.client.connected:
            return True
        await self.connect()
        return self.connected

    def _update_failure(self, error: str):
        self.last_error = error
        self.failure_count += 1
        self.connected = False
        if self.failure_count >= self.cb_fail_threshold:
            self.cb_open_until = time.time() + self.cb_cooldown
            logger.error("modbus_circuit_opened", cooldown=self.cb_cooldown)
            MODBUS_CIRCUIT_OPEN.inc()
            self.failure_count = 0

    def get_status(self) -> dict:
        """返回连接状态摘要，供健康检查使用"""
        return {
            "connected": bool(self.connected),
            "last_success_ts": self.last_success_ts,
            "last_error": self.last_error,
            "failure_count": self.failure_count,
            "reconnect_count": self.reconnect_count,
            "host": self.host,
            "port": self.port,
            "slave_id": self.slave_id,
        }

    async def read_registers(self, address: int, count: int = 1, function_code: int = 3, retries: int = 2):
        """
        通用寄存器读取

        Args:
            address: 起始地址
            count: 数量
            function_code: 3=保持寄存器, 4=输入寄存器
            retries: 重试次数
        """
        attempt = 0
        while attempt <= retries:
            attempt += 1
            backoff = min(0.2 * (2 ** (attempt - 1)), 2.0)

            if not await self.ensure_connected():
                logger.warning("modbus_not_connected")
                await asyncio.sleep(backoff)
                continue

            try:
                start = time.time()
                if function_code == 3:
                    result = await self.client.read_holding_registers(
                        address=address,
                        count=count,
                        slave=self.slave_id,
                    )
                elif function_code == 4:
                    result = await self.client.read_input_registers(
                        address=address,
                        count=count,
                        slave=self.slave_id,
                    )
                else:
                    logger.error("modbus_function_not_supported", function_code=function_code)
                    return None

                if result.isError():
                    logger.error(
                        "modbus_read_error",
                        address=address,
                        count=count,
                        function_code=function_code,
                        error=str(result),
                    )
                    self._update_failure(str(result))
                    MODBUS_READ_FAILURE.labels(str(function_code)).inc()
                    continue

                self.last_success_ts = time.time()
                MODBUS_READ_SUCCESS.labels(str(function_code)).inc()
                MODBUS_LAST_LATENCY_MS.labels("read").set((time.time() - start) * 1000)
                return result.registers

            except ModbusException as e:
                logger.error(
                    "modbus_exception",
                    address=address,
                    count=count,
                    function_code=function_code,
                    error=str(e),
                )
                self._update_failure(str(e))
                MODBUS_READ_FAILURE.labels(str(function_code)).inc()
            except (asyncio.TimeoutError, AttributeError, OSError, RuntimeError, TypeError, ValueError) as e:
                logger.error("modbus_read_unexpected_error", address=address, error=str(e))
                self._update_failure(str(e))
                MODBUS_READ_FAILURE.labels(str(function_code)).inc()

            await asyncio.sleep(backoff)

        return None

    async def read_holding_registers(self, address: int, count: int = 1, retries: int = 2):
        """向后兼容的保持寄存器读取封装"""
        return await self.read_registers(address=address, count=count, function_code=3, retries=retries)

    async def read_bits(self, address: int, count: int = 1, function_code: int = 1, retries: int = 2):
        """
        读取位（线圈/离散输入）

        Args:
            function_code: 1=读线圈(0x01), 2=读离散输入(0x02)
        Returns:
            bool 列表（长度 count），失败返回 None
        """
        attempt = 0
        while attempt <= retries:
            attempt += 1
            backoff = min(0.2 * (2 ** (attempt - 1)), 2.0)

            if not await self.ensure_connected():
                logger.warning("modbus_not_connected")
                await asyncio.sleep(backoff)
                continue

            try:
                start = time.time()
                if function_code == 1:
                    result = await self.client.read_coils(address=address, count=count, slave=self.slave_id)
                elif function_code == 2:
                    result = await self.client.read_discrete_inputs(address=address, count=count, slave=self.slave_id)
                else:
                    logger.error("modbus_bit_function_not_supported", function_code=function_code)
                    return None

                if result.isError():
                    logger.error("modbus_read_error", address=address, count=count, function_code=function_code, error=str(result))
                    self._update_failure(str(result))
                    MODBUS_READ_FAILURE.labels(str(function_code)).inc()
                    continue

                self.last_success_ts = time.time()
                MODBUS_READ_SUCCESS.labels(str(function_code)).inc()
                MODBUS_LAST_LATENCY_MS.labels("read").set((time.time() - start) * 1000)
                return [bool(b) for b in list(result.bits)[:count]]

            except ModbusException as e:
                logger.error("modbus_exception", address=address, function_code=function_code, error=str(e))
                self._update_failure(str(e))
                MODBUS_READ_FAILURE.labels(str(function_code)).inc()
            except (asyncio.TimeoutError, AttributeError, OSError, RuntimeError, TypeError, ValueError) as e:
                logger.error("modbus_read_unexpected_error", address=address, error=str(e))
                self._update_failure(str(e))
                MODBUS_READ_FAILURE.labels(str(function_code)).inc()

            await asyncio.sleep(backoff)

        return None

    async def write_register(self, address: int, value: int, retries: int = 2):
        """
        写入单个寄存器

        Args:
            address: 寄存器地址
            value: 写入的值
            retries: 重试次数
        """
        attempt = 0
        while attempt <= retries:
            attempt += 1
            backoff = min(0.2 * (2 ** (attempt - 1)), 2.0)

            if not await self.ensure_connected():
                logger.warning("modbus_not_connected")
                await asyncio.sleep(backoff)
                continue

            try:
                start = time.time()
                result = await self.client.write_register(
                    address=address,
                    value=value,
                    slave=self.slave_id,
                )

                if result.isError():
                    logger.error(
                        "modbus_write_error",
                        address=address,
                        value=value,
                        error=str(result),
                    )
                    self._update_failure(str(result))
                    MODBUS_WRITE_FAILURE.labels("single").inc()
                    continue

                logger.debug(
                    "modbus_write_success",
                    address=address,
                    value=value,
                )
                self.last_success_ts = time.time()
                MODBUS_WRITE_SUCCESS.labels("single").inc()
                MODBUS_LAST_LATENCY_MS.labels("write").set((time.time() - start) * 1000)
                return True

            except ModbusException as e:
                logger.error(
                    "modbus_write_exception",
                    address=address,
                    error=str(e),
                )
                self._update_failure(str(e))
            except (asyncio.TimeoutError, AttributeError, OSError, RuntimeError, TypeError, ValueError) as e:
                logger.error("modbus_write_unexpected_error", address=address, error=str(e))
                self._update_failure(str(e))

            await asyncio.sleep(backoff)

        return False

    async def write_registers(self, address: int, values: list[int], retries: int = 2):
        """
        写多个寄存器（功能码 16）
        """
        attempt = 0
        while attempt <= retries:
            attempt += 1
            backoff = min(0.2 * (2 ** (attempt - 1)), 2.0)

            if not await self.ensure_connected():
                logger.warning("modbus_not_connected")
                await asyncio.sleep(backoff)
                continue

            try:
                start = time.time()
                result = await self.client.write_registers(
                    address=address,
                    values=values,
                    slave=self.slave_id,
                )
                if result.isError():
                    logger.error(
                        "modbus_write_error",
                        address=address,
                        value=values,
                        error=str(result),
                    )
                    self._update_failure(str(result))
                    MODBUS_WRITE_FAILURE.labels("multi").inc()
                    continue

                logger.debug("modbus_write_registers_success", address=address, values=values)
                self.last_success_ts = time.time()
                MODBUS_WRITE_SUCCESS.labels("multi").inc()
                MODBUS_LAST_LATENCY_MS.labels("write").set((time.time() - start) * 1000)
                return True
            except ModbusException as e:
                logger.error("modbus_write_exception", address=address, error=str(e))
                self._update_failure(str(e))
                MODBUS_WRITE_FAILURE.labels("multi").inc()
            except (asyncio.TimeoutError, AttributeError, OSError, RuntimeError, TypeError, ValueError) as e:
                logger.error("modbus_write_unexpected_error", address=address, error=str(e))
                self._update_failure(str(e))
                MODBUS_WRITE_FAILURE.labels("multi").inc()

            await asyncio.sleep(backoff)

        return False

    async def write_float(self, address: int, value: float, byte_order: str = "big", retries: int = 2):
        """
        写入浮点数（两个寄存器）
        """
        import struct
        endian = ">" if byte_order == "big" else "<"
        hi, lo = struct.unpack(f"{endian}HH", struct.pack(f"{endian}f", float(value)))
        return await self.write_registers(address=address, values=[hi, lo], retries=retries)

    async def write_int32(self, address: int, value: int, byte_order: str = "big", retries: int = 2):
        """
        写入 32 位整数（两个寄存器）
        """
        endian = ">" if byte_order == "big" else "<"
        import struct
        hi, lo = struct.unpack(f"{endian}HH", struct.pack(f"{endian}I", int(value) & 0xFFFFFFFF))
        return await self.write_registers(address=address, values=[hi, lo], retries=retries)

    async def read_float(self, address: int, byte_order: str = "big", function_code: int = 3):
        """
        读取浮点数（2个寄存器）
        """
        registers = await self.read_registers(address, 2, function_code=function_code)

        if registers is None or len(registers) != 2:
            return None

        import struct
        endian = ">" if byte_order == "big" else "<"
        bytes_data = struct.pack(f"{endian}HH", registers[0], registers[1])
        value = struct.unpack(f"{endian}f", bytes_data)[0]

        return value

    async def read_int32(self, address: int, signed: bool = True, byte_order: str = "big", function_code: int = 3):
        """
        读取 32 位整数（两个寄存器）
        """
        registers = await self.read_registers(address, 2, function_code=function_code)
        if registers is None or len(registers) != 2:
            return None
        import struct
        endian = ">" if byte_order == "big" else "<"
        fmt = f"{endian}{'i' if signed else 'I'}"
        bytes_data = struct.pack(f"{endian}HH", registers[0], registers[1])
        return struct.unpack(fmt, bytes_data)[0]
