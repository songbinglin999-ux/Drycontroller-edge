"""
PLC 模拟器 - 用于开发和测试
"""

import random
import structlog
import math
import time

logger = structlog.get_logger(__name__)


class PLCSimulator:
    """PLC 模拟器"""
    
    def __init__(self, config):
        self.config = config
        self.start_time = time.time()
        self.data = {}
        
        # 初始化模拟数据
        self._initialize_data()
        
        logger.info("plc_simulator_initialized")
    
    def _initialize_data(self):
        """初始化模拟数据"""
        # 第一段烘干机
        self.data.update({
            40001: 85.0,   # DRYER1.IN.TEMP
            40002: 78.0,   # DRYER1.OUT.TEMP
            40003: 82.0,   # DRYER1.CHAMBER.TEMP
            40010: 4.2,    # DRYER1.CHAIN.SPEED
            40011: 35.0,   # DRYER1.FAN.SPEED
            40012: 250.0,  # DRYER1.TORQUE
        })
        
        # 第二段烘干机
        self.data.update({
            40021: 65.0,   # DRYER2.IN.TEMP
            40022: 55.0,   # DRYER2.OUT.TEMP
            40023: 60.0,   # DRYER2.CHAMBER.TEMP
            40030: 4.5,    # DRYER2.CHAIN.SPEED
            40031: 30.0,   # DRYER2.FAN.SPEED
            40032: 220.0,  # DRYER2.TORQUE
        })
        
        # 发酵仓
        self.data.update({
            40041: 40.0,   # FERM.CHAMBER.TEMP
            40042: 65.0,   # FERM.HUMIDITY
        })
        
        # 尾气余热
        self.data.update({
            40051: 180.0,  # EXHAUST.TEMP
            40052: 50.0,   # EXHAUST.RECOVERY.VALVE
        })
    
    async def read_float(self, address: int):
        """
        读取浮点数
        
        Args:
            address: 寄存器地址
            
        Returns:
            模拟的浮点数值
        """
        # 获取基础值
        base_value = self.data.get(address, 0.0)
        
        # 添加随机波动
        noise = random.uniform(-0.5, 0.5)
        
        # 添加周期性变化（模拟实际工艺波动）
        elapsed = time.time() - self.start_time
        periodic = math.sin(elapsed / 60.0) * 2.0
        
        value = base_value + noise + periodic
        
        # 确保值在合理范围内
        value = max(0, value)
        
        return value
    
    async def read_holding_registers(self, address: int, count: int = 1):
        """
        读取保持寄存器
        
        Args:
            address: 起始地址
            count: 读取数量
            
        Returns:
            寄存器值列表
        """
        registers = []
        
        for i in range(count):
            addr = address + i
            value = await self.read_float(addr)
            # 转换为整数
            registers.append(int(value * 10))  # 保留一位小数
        
        return registers
    
    async def write_register(self, address: int, value: int):
        """
        写入寄存器（模拟控制）
        
        Args:
            address: 寄存器地址
            value: 写入的值
            
        Returns:
            是否成功
        """
        self.data[address] = float(value)
        
        logger.info(
            "simulator_write",
            address=address,
            value=value,
        )
        
        return True

