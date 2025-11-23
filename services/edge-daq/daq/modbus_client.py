"""
Modbus TCP 客户端
"""

import asyncio
import structlog
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

logger = structlog.get_logger(__name__)


class ModbusClient:
    """Modbus TCP 客户端封装"""
    
    def __init__(self, host: str, port: int = 502, slave_id: int = 1):
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self.client = None
        self.connected = False
    
    async def connect(self):
        """连接到 Modbus 设备"""
        try:
            self.client = AsyncModbusTcpClient(
                host=self.host,
                port=self.port,
                timeout=5,
            )
            
            await self.client.connect()
            self.connected = self.client.connected
            
            if self.connected:
                logger.info(
                    "modbus_connected",
                    host=self.host,
                    port=self.port,
                    slave_id=self.slave_id,
                )
            else:
                logger.error("modbus_connection_failed")
        
        except Exception as e:
            logger.error("modbus_connect_error", error=str(e))
            self.connected = False
    
    async def disconnect(self):
        """断开连接"""
        if self.client:
            self.client.close()
            self.connected = False
            logger.info("modbus_disconnected")
    
    async def read_holding_registers(self, address: int, count: int = 1):
        """
        读取保持寄存器
        
        Args:
            address: 起始地址
            count: 读取数量
            
        Returns:
            寄存器值列表
        """
        if not self.connected:
            logger.warning("modbus_not_connected")
            return None
        
        try:
            result = await self.client.read_holding_registers(
                address=address,
                count=count,
                slave=self.slave_id,
            )
            
            if result.isError():
                logger.error(
                    "modbus_read_error",
                    address=address,
                    count=count,
                    error=str(result),
                )
                return None
            
            return result.registers
        
        except ModbusException as e:
            logger.error(
                "modbus_exception",
                address=address,
                error=str(e),
            )
            return None
    
    async def write_register(self, address: int, value: int):
        """
        写入单个寄存器
        
        Args:
            address: 寄存器地址
            value: 写入的值
            
        Returns:
            是否成功
        """
        if not self.connected:
            logger.warning("modbus_not_connected")
            return False
        
        try:
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
                return False
            
            logger.debug(
                "modbus_write_success",
                address=address,
                value=value,
            )
            return True
        
        except ModbusException as e:
            logger.error(
                "modbus_write_exception",
                address=address,
                error=str(e),
            )
            return False
    
    async def read_float(self, address: int):
        """
        读取浮点数（2个寄存器）
        
        Args:
            address: 起始地址
            
        Returns:
            浮点数值
        """
        registers = await self.read_holding_registers(address, 2)
        
        if registers is None or len(registers) != 2:
            return None
        
        # 将两个16位寄存器转换为32位浮点数
        # 这里假设高位在前（big-endian）
        import struct
        bytes_data = struct.pack('>HH', registers[0], registers[1])
        value = struct.unpack('>f', bytes_data)[0]
        
        return value

