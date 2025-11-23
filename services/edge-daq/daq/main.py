"""
边缘数据采集服务主入口
"""

import asyncio
import signal
import structlog
import yaml
import os
from pathlib import Path

from daq.modbus_client import ModbusClient
from daq.plc_reader import PLCReader
from daq.publisher import DataPublisher
from daq.simulator import PLCSimulator

# 设置日志
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
)

logger = structlog.get_logger(__name__)


class DAQService:
    """数据采集服务"""
    
    def __init__(self):
        self.running = False
        self.modbus_client = None
        self.plc_reader = None
        self.publisher = None
        self.simulator = None
        self.config = None
        
    async def load_config(self):
        """加载配置文件"""
        config_path = Path("/app/configs/tags.yaml")
        
        if not config_path.exists():
            logger.warning("config_file_not_found", path=str(config_path))
            return {}
        
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)
        
        logger.info("config_loaded", tags_count=len(self.config.get("tags", {})))
        return self.config
    
    async def start(self):
        """启动服务"""
        logger.info("starting_daq_service")
        
        # 加载配置
        await self.load_config()
        
        # 获取环境变量
        plc_host = os.getenv("PLC_HOST", "192.168.1.100")
        plc_port = int(os.getenv("PLC_PORT", "502"))
        use_simulator = os.getenv("USE_SIMULATOR", "true").lower() == "true"
        
        # 初始化发布器
        self.publisher = DataPublisher()
        await self.publisher.connect()
        
        # 根据配置选择使用真实 PLC 或模拟器
        if use_simulator:
            logger.info("using_plc_simulator")
            self.simulator = PLCSimulator(self.config)
            self.plc_reader = PLCReader(self.simulator, self.publisher, self.config)
        else:
            logger.info("connecting_to_plc", host=plc_host, port=plc_port)
            self.modbus_client = ModbusClient(plc_host, plc_port)
            await self.modbus_client.connect()
            self.plc_reader = PLCReader(self.modbus_client, self.publisher, self.config)
        
        # 启动数据采集
        self.running = True
        await self.plc_reader.start()
        
        logger.info("daq_service_started")
    
    async def stop(self):
        """停止服务"""
        logger.info("stopping_daq_service")
        
        self.running = False
        
        if self.plc_reader:
            await self.plc_reader.stop()
        
        if self.publisher:
            await self.publisher.disconnect()
        
        if self.modbus_client:
            await self.modbus_client.disconnect()
        
        logger.info("daq_service_stopped")


async def main():
    """主函数"""
    service = DAQService()
    
    # 注册信号处理
    loop = asyncio.get_event_loop()
    
    def signal_handler():
        logger.info("received_shutdown_signal")
        asyncio.create_task(service.stop())
    
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)
    
    try:
        await service.start()
        
        # 保持运行
        while service.running:
            await asyncio.sleep(1)
    
    except Exception as e:
        logger.error("service_error", error=str(e), exc_info=True)
    
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())

