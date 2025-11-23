"""
AI 软测量服务 - 含水率预测
"""

import asyncio
import signal
import structlog
import yaml
import os
from pathlib import Path

from src.infer import SoftSensorModel
from src.mqtt_handler import MQTTHandler

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
)

logger = structlog.get_logger(__name__)


class SoftSensorService:
    """软测量服务"""
    
    def __init__(self):
        self.running = False
        self.model = None
        self.mqtt_handler = None
        self.config = None
    
    async def load_config(self):
        """加载配置"""
        config_path = Path("/app/configs/ai-config.yaml")
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)
        
        logger.info("config_loaded")
        return self.config
    
    async def start(self):
        """启动服务"""
        logger.info("starting_softsensor_service")
        
        await self.load_config()
        
        # 初始化模型
        model_config = self.config.get("softsensor", {})
        self.model = SoftSensorModel(model_config)
        await self.model.load()
        
        # 初始化 MQTT 处理器
        self.mqtt_handler = MQTTHandler(self.model, self.config)
        await self.mqtt_handler.connect()
        
        self.running = True
        logger.info("softsensor_service_started")
    
    async def stop(self):
        """停止服务"""
        logger.info("stopping_softsensor_service")
        
        self.running = False
        
        if self.mqtt_handler:
            await self.mqtt_handler.disconnect()
        
        logger.info("softsensor_service_stopped")


async def main():
    """主函数"""
    service = SoftSensorService()
    
    loop = asyncio.get_event_loop()
    
    def signal_handler():
        logger.info("received_shutdown_signal")
        asyncio.create_task(service.stop())
    
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)
    
    try:
        await service.start()
        
        while service.running:
            await asyncio.sleep(1)
    
    except Exception as e:
        logger.error("service_error", error=str(e), exc_info=True)
    
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())

