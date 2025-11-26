"""
AI 软测量服务 - 含水率预测
"""

import asyncio
import signal
import structlog
import yaml
import os
import time
from pathlib import Path

from aiohttp import web

from common.logging import setup_logging
from src.infer import SoftSensorModel
from src.mqtt_handler import MQTTHandler

event_recorder = setup_logging("ai-softsensor")
logger = structlog.get_logger(__name__)


class SoftSensorService:
    """软测量服务"""
    
    def __init__(self):
        self.running = False
        self.model = None
        self.mqtt_handler = None
        self.config = None
        self.health_runner = None
        self.health_site = None
        self.health_port = int(os.getenv("HEALTH_PORT", "8080"))
    
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
        event_recorder.record_event(
            "service_starting",
            tags={"component": "ai-softsensor"},
        )
        
        await self.load_config()
        await self._start_health_server()
        
        # 初始化模型
        model_config = self.config.get("softsensor", {})
        self.model = SoftSensorModel(model_config)
        await self.model.load()
        
        # 初始化 MQTT 处理器
        self.mqtt_handler = MQTTHandler(self.model, self.config)
        await self.mqtt_handler.connect()
        
        self.running = True
        logger.info("softsensor_service_started")
        event_recorder.record_event(
            "service_started",
            tags={"component": "ai-softsensor"},
        )
    
    async def stop(self):
        """停止服务"""
        logger.info("stopping_softsensor_service")
        event_recorder.record_event(
            "service_stopping",
            tags={"component": "ai-softsensor"},
        )
        
        self.running = False
        
        if self.mqtt_handler:
            await self.mqtt_handler.disconnect()

        await self._stop_health_server()
        
        logger.info("softsensor_service_stopped")
        event_recorder.record_event(
            "service_stopped",
            tags={"component": "ai-softsensor"},
        )

    async def _start_health_server(self):
        if self.health_runner:
            return

        app = web.Application()

        async def health_handler(request):
            return web.json_response(self._health_payload())

        app.router.add_get("/health", health_handler)
        self.health_runner = web.AppRunner(app)
        await self.health_runner.setup()
        self.health_site = web.TCPSite(self.health_runner, "0.0.0.0", self.health_port)
        await self.health_site.start()
        logger.info("health_server_started", port=self.health_port)

    async def _stop_health_server(self):
        if self.health_runner:
            await self.health_runner.cleanup()
            self.health_runner = None
            self.health_site = None
            logger.info("health_server_stopped")

    def _health_payload(self):
        return {
            "success": True,
            "service": "ai-softsensor",
            "status": "running" if self.running else "starting",
            "timestamp": int(time.time()),
            "details": {
                "model_loaded": self.model is not None,
                "mqtt_connected": bool(self.mqtt_handler and self.mqtt_handler.client),
            },
        }


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
        event_recorder.record_event(
            "service_error",
            severity="error",
            fields={"error": str(e)},
            tags={"component": "ai-softsensor"},
        )
    
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())

