"""
PLC 数据读取器

本模块负责从 PLC 控制器周期性读取数据，并将数据发布到消息队列和时序数据库。

主要功能：
- 按配置的扫描周期轮询 PLC 数据
- 支持 Modbus TCP 协议读取
- 自动检测报警条件并发布报警消息
- 数据范围校验

采集流程：
1. 按配置的 scan_interval 触发扫描
2. 遍历所有配置的标签点
3. 通过 Modbus 读取寄存器值
4. 校验范围和报警阈值
5. 发布到 MQTT 和 InfluxDB
"""

import asyncio
import structlog
import time

logger = structlog.get_logger(__name__)


class PLCReader:
    """
    PLC 数据读取器
    
    负责周期性采集 PLC 数据并发布到消息队列和数据库。
    
    Attributes:
        modbus_client: Modbus 客户端（或模拟器）
        publisher: 数据发布器
        config: 标签配置
        scan_interval: 扫描周期（秒）
    """
    
    def __init__(self, modbus_client, publisher, config):
        """
        初始化读取器
        
        Args:
            modbus_client: Modbus 客户端实例
            publisher: 数据发布器实例
            config: 标签配置字典
        """
        self.modbus_client = modbus_client
        self.publisher = publisher
        self.config = config
        self.running = False
        # 从配置读取扫描间隔，默认 1 秒
        self.scan_interval = config.get("acquisition", {}).get("scan_interval", 1.0)
    
    async def start(self):
        """
        启动数据采集
        
        创建后台任务执行周期性扫描
        """
        self.running = True
        logger.info("plc_reader_started", scan_interval=self.scan_interval)
        
        # 启动采集循环任务
        asyncio.create_task(self._scan_loop())
    
    async def stop(self):
        """停止数据采集"""
        self.running = False
        logger.info("plc_reader_stopped")
    
    async def _scan_loop(self):
        """
        数据采集主循环
        
        按固定周期执行扫描，自动补偿采集耗时
        """
        while self.running:
            start_time = time.time()
            
            try:
                # 读取所有配置的标签
                await self._read_all_tags()
            
            except Exception as e:
                logger.error("scan_error", error=str(e), exc_info=True)
            
            # 计算实际耗时，补偿睡眠时间以保持稳定的扫描周期
            elapsed = time.time() - start_time
            sleep_time = max(0, self.scan_interval - elapsed)
            
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
    
    async def _read_all_tags(self):
        """
        读取所有配置的标签数据
        
        遍历 tags.yaml 中定义的所有标签，按分组/类别组织
        """
        tags_config = self.config.get("tags", {})
        
        # 遍历标签配置：section（分区）-> category（类别）-> tags（标签列表）
        for section, categories in tags_config.items():
            if not isinstance(categories, dict):
                continue
            
            for category, tags in categories.items():
                if not isinstance(tags, list):
                    continue
                
                for tag_config in tags:
                    await self._read_tag(tag_config)
    
    async def _read_tag(self, tag_config: dict):
        """
        读取单个标签的值
        
        处理流程：
        1. 从配置获取标签元信息
        2. 通过 Modbus 读取寄存器值
        3. 校验数值范围
        4. 检查报警条件
        5. 发布数据
        
        Args:
            tag_config: 标签配置字典，包含：
                - name: 标签名称
                - address: Modbus 寄存器地址
                - type: 数据类型（float/int）
                - unit: 工程单位
                - range: 有效范围 [min, max]
                - alarm_high/alarm_low: 报警阈值
        """
        tag_name = tag_config.get("name")
        address = tag_config.get("address")
        tag_type = tag_config.get("type", "float")
        unit = tag_config.get("unit", "")
        
        # 配置校验
        if not tag_name or address is None:
            return
        
        try:
            # 根据数据类型选择读取方式
            if tag_type == "float":
                # 浮点数需要读取两个连续寄存器
                value = await self.modbus_client.read_float(address)
            else:
                # 整数只需读取一个寄存器
                registers = await self.modbus_client.read_holding_registers(address, 1)
                value = registers[0] if registers else None
            
            if value is not None:
                # 范围校验：检查值是否在有效范围内
                value_range = tag_config.get("range")
                if value_range:
                    min_val, max_val = value_range
                    if value < min_val or value > max_val:
                        logger.warning(
                            "value_out_of_range",
                            tag=tag_name,
                            value=value,
                            range=value_range,
                        )
                
                # 报警检测：检查是否超过高/低阈值
                alarm_high = tag_config.get("alarm_high")
                alarm_low = tag_config.get("alarm_low")
                
                # 高报警检测
                if alarm_high and value > alarm_high:
                    logger.warning(
                        "alarm_high_triggered",
                        tag=tag_name,
                        value=value,
                        threshold=alarm_high,
                    )
                    await self._publish_alarm(tag_name, "high", value, alarm_high)
                
                # 低报警检测
                if alarm_low and value < alarm_low:
                    logger.warning(
                        "alarm_low_triggered",
                        tag=tag_name,
                        value=value,
                        threshold=alarm_low,
                    )
                    await self._publish_alarm(tag_name, "low", value, alarm_low)
                
                # 发布采集数据到 MQTT 和 InfluxDB
                await self.publisher.publish_tag_data(
                    tag_name=tag_name,
                    value=value,
                    unit=unit,
                    timestamp=time.time(),
                )
        
        except Exception as e:
            logger.error(
                "tag_read_error",
                tag=tag_name,
                address=address,
                error=str(e),
            )
    
    async def _publish_alarm(self, tag_name: str, alarm_type: str, value: float, threshold: float):
        """
        发布报警消息
        
        Args:
            tag_name: 触发报警的标签名
            alarm_type: 报警类型（"high" 或 "low"）
            value: 当前值
            threshold: 触发的阈值
        """
        await self.publisher.publish_alarm(
            tag_name=tag_name,
            alarm_type=alarm_type,
            value=value,
            threshold=threshold,
        )

