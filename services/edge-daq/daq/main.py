"""
边缘数据采集服务主入口
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

event_recorder = setup_logging("edge-daq")
logger = structlog.get_logger(__name__)

from daq.modbus_client import ModbusClient
from daq.plc_reader import PLCReader
from daq.publisher import DataPublisher
from daq.simulator import PLCSimulator


class DAQService:
    """数据采集服务"""
    
    def __init__(self):
        self.running = False
        self.modbus_client = None
        self.plc_reader = None
        self.publisher = None
        self.simulator = None
        self.config = None
        self.health_runner = None
        self.health_site = None
        self.health_port = int(os.getenv("HEALTH_PORT", "8080"))
        
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
        event_recorder.record_event(
            "service_starting",
            tags={"component": "edge-daq"},
        )
        
        # 加载配置
        await self.load_config()

        # 启动健康检查 HTTP 服务
        await self._start_health_server()
        
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
        event_recorder.record_event(
            "service_started",
            tags={"component": "edge-daq"},
        )
    
    async def stop(self):
        """停止服务"""
        logger.info("stopping_daq_service")
        event_recorder.record_event(
            "service_stopping",
            tags={"component": "edge-daq"},
        )
        
        self.running = False
        
        if self.plc_reader:
            await self.plc_reader.stop()
        
        if self.publisher:
            await self.publisher.disconnect()
        
        if self.modbus_client:
            await self.modbus_client.disconnect()

        await self._stop_health_server()
        
        logger.info("daq_service_stopped")
        event_recorder.record_event(
            "service_stopped",
            tags={"component": "edge-daq"},
        )

    async def _start_health_server(self):
        """启动健康检查服务器"""
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
        """构建健康状态"""
        return {
            "success": True,
            "service": "edge-daq",
            "status": "running" if self.running else "starting",
            "timestamp": int(time.time()),
            "details": {
                "publisher_connected": bool(self.publisher and self.publisher.connected),
                "use_simulator": self.simulator is not None,
            },
        }


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
        event_recorder.record_event(
            "service_error",
            severity="error",
            fields={"error": str(e)},
            tags={"component": "edge-daq"},
        )
    
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())

