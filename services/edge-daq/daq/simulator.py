"""
PLC 模拟器 - 用于开发和测试
"""

import random
import struct
import structlog
import math
import time

logger = structlog.get_logger(__name__)


class PLCSimulator:
    """PLC 模拟器"""
    
    def __init__(self, config):
        self.config = config or {}
        self.start_time = time.time()
        self.data = {}
        self.tags_by_address = {}
        
        # 初始化模拟数据
        self._initialize_data()
        
        logger.info("plc_simulator_initialized", tags=len(self.tags_by_address))
    
    def _initialize_data(self):
        """初始化模拟数据"""
        next_address = 40001
        for index, tag in enumerate(self._iter_tag_items(self.config.get("tags", {}))):
            if not tag.get("name"):
                continue

            address = self._coerce_address(tag.get("address"))
            tag_type = str(tag.get("type", "float")).lower()
            width = self._register_width(tag_type)

            if address is None:
                address = next_address
                tag["address"] = address
                tag.setdefault("function_code", 3)
                tag.setdefault("byte_order", "big")
                tag.setdefault("scale", 1.0)

            next_address = max(next_address, address + width)
            self.tags_by_address[address] = tag
            self.data[address] = self._default_value_for_tag(tag, index)

    def _iter_tag_items(self, node):
        if isinstance(node, dict):
            if isinstance(node.get("name"), str):
                yield node
            for value in node.values():
                yield from self._iter_tag_items(value)
        elif isinstance(node, list):
            for item in node:
                yield from self._iter_tag_items(item)

    def _coerce_address(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _register_width(self, tag_type: str):
        return 2 if tag_type in {"float", "int32", "uint32"} else 1

    def _default_value_for_tag(self, tag, index: int):
        name = str(tag.get("name", ""))
        tag_type = str(tag.get("type", "")).lower()
        unit = tag.get("unit", "")

        if tag_type == "bool":
            return 1.0 if index % 4 else 0.0
        if name.startswith("FTT"):
            return 78.0 + (index % 3) * 4.0
        if name.startswith("TT"):
            return 155.0 + (index % 2) * 10.0
        if name == "MT1":
            return 13.5
        if name.startswith("I_M"):
            return 12.0 + (index % 5) * 3.0
        if name.startswith("VFM") or unit == "%/Hz":
            return 42.0 + (index % 3) * 5.0
        if unit == "%":
            return 50.0
        if unit == "℃":
            return 80.0
        return 1.0

    def _current_value(self, address: int):
        tag = self.tags_by_address.get(address, {})
        tag_type = str(tag.get("type", "")).lower()
        base_value = self.data.get(address, 0.0)

        if tag_type == "bool":
            return int(base_value)

        noise = random.uniform(-0.5, 0.5)
        elapsed = time.time() - self.start_time
        periodic = math.sin(elapsed / 60.0) * 2.0
        return max(0, base_value + noise + periodic)

    def _encode_registers(self, address: int):
        tag = self.tags_by_address.get(address, {})
        tag_type = str(tag.get("type", "float")).lower()
        byte_order = tag.get("byte_order", "big")
        endian = ">" if byte_order == "big" else "<"
        value = self._current_value(address)

        if tag_type == "float":
            return list(struct.unpack(f"{endian}HH", struct.pack(f"{endian}f", float(value))))
        if tag_type in {"int32", "uint32"}:
            fmt = "i" if tag_type == "int32" else "I"
            return list(struct.unpack(f"{endian}HH", struct.pack(f"{endian}{fmt}", int(value))))
        if tag_type == "bool":
            return [1 if value else 0]
        return [int(value)]
    
    async def read_float(self, address: int, byte_order: str = "big", function_code: int = 3):
        """
        读取浮点数
        
        Args:
            address: 寄存器地址
            
        Returns:
            模拟的浮点数值
        """
        return self._current_value(address)
    
    async def read_registers(self, address: int, count: int = 1, function_code: int = 3):
        """
        读取寄存器
        
        Args:
            address: 起始地址
            count: 读取数量
            
        Returns:
            寄存器值列表
        """
        registers = []
        cursor = address

        while len(registers) < count:
            encoded = self._encode_registers(cursor)
            registers.extend(encoded)
            cursor += max(1, len(encoded))
        
        return registers[:count]

    async def read_holding_registers(self, address: int, count: int = 1):
        """读取保持寄存器"""
        return await self.read_registers(address, count, function_code=3)
    
    async def write_register(self, address: int, value: int, retries: int = 2):
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

    async def write_float(self, address: int, value: float, byte_order: str = "big", retries: int = 2):
        """写入浮点数（模拟控制）"""
        self.data[address] = float(value)
        logger.info("simulator_write_float", address=address, value=value, byte_order=byte_order)
        return True

    async def write_int32(self, address: int, value: int, byte_order: str = "big", retries: int = 2):
        """写入 32 位整数（模拟控制）"""
        self.data[address] = int(value)
        logger.info("simulator_write_int32", address=address, value=value, byte_order=byte_order)
        return True
