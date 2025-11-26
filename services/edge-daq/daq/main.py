"""
边缘数据采集服务主入口

本模块是 edge-daq 服务的启动入口，负责：
- 连接 PLC 控制器（或使用模拟器）
- 周期性采集工业现场数据
- 将数据发布到 MQTT 和 InfluxDB

服务架构：
┌─────────────┐    ┌──────────────┐    ┌─────────────┐
│   PLC/模拟器  │ -> │  PLCReader   │ -> │ DataPublisher│
│  (数据源)    │    │  (采集调度)   │    │ (MQTT+Influx)│
└─────────────┘    └──────────────┘    └─────────────┘

环境变量配置：
- PLC_HOST: PLC 地址（默认 192.168.1.100）
- PLC_PORT: Modbus 端口（默认 502）
- USE_SIMULATOR: 是否使用模拟器（默认 true）
- HEALTH_PORT: 健康检查端口（默认 8080）
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
    """
    数据采集服务主类
    
    负责协调 PLC 连接、数据采集和发布的全流程。
    支持真实 PLC 和模拟器两种数据源模式。
    
    Attributes:
        running: 服务运行状态标志
        modbus_client: Modbus TCP 客户端（真实 PLC 模式）
        plc_reader: PLC 数据读取器
        publisher: 数据发布器（MQTT + InfluxDB）
        simulator: PLC 模拟器（模拟模式）
        config: 标签配置信息
    """
    
    def __init__(self):
        """初始化采集服务"""
        self.running = False
        self.modbus_client = None
        self.plc_reader = None
        self.publisher = None
        self.simulator = None
        self.config = None
        # 健康检查 HTTP 服务器
        self.health_runner = None
        self.health_site = None
        self.health_port = int(os.getenv("HEALTH_PORT", "8080"))
        
    async def load_config(self):
        """
        加载标签配置文件
        
        配置文件定义了需要采集的 PLC 标签点，包括：
        - 标签名称和地址映射
        - 数据类型和单位
        - 报警阈值
        
        Returns:
            加载的配置字典
        """
        config_path = Path("/app/configs/tags.yaml")
        
        if not config_path.exists():
            logger.warning("config_file_not_found", path=str(config_path))
            return {}
        
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)
        
        logger.info("config_loaded", tags_count=len(self.config.get("tags", {})))
        return self.config
    
    async def start(self):
        """
        启动数据采集服务
        
        启动流程：
        1. 加载标签配置
        2. 启动健康检查服务
        3. 初始化数据发布器
        4. 连接 PLC 或启动模拟器
        5. 启动周期性采集循环
        """
        logger.info("starting_daq_service")
        event_recorder.record_event(
            "service_starting",
            tags={"component": "edge-daq"},
        )
        
        # 加载标签配置
        await self.load_config()

        # 启动健康检查 HTTP 服务
        await self._start_health_server()
        
        # 读取 PLC 连接配置
        plc_host = os.getenv("PLC_HOST", "192.168.1.100")
        plc_port = int(os.getenv("PLC_PORT", "502"))
        use_simulator = os.getenv("USE_SIMULATOR", "true").lower() == "true"
        
        # 初始化数据发布器（连接 MQTT 和 InfluxDB）
        self.publisher = DataPublisher()
        await self.publisher.connect()
        
        # 根据配置选择数据源：模拟器或真实 PLC
        if use_simulator:
            logger.info("using_plc_simulator")
            self.simulator = PLCSimulator(self.config)
            self.plc_reader = PLCReader(self.simulator, self.publisher, self.config)
        else:
            logger.info("connecting_to_plc", host=plc_host, port=plc_port)
            self.modbus_client = ModbusClient(plc_host, plc_port)
            await self.modbus_client.connect()
            self.plc_reader = PLCReader(self.modbus_client, self.publisher, self.config)
        
        # 启动数据采集循环
        self.running = True
        await self.plc_reader.start()
        
        logger.info("daq_service_started")
        event_recorder.record_event(
            "service_started",
            tags={"component": "edge-daq"},
        )
    
    async def stop(self):
        """
        停止数据采集服务
        
        按顺序停止：采集器 -> 发布器 -> PLC 连接 -> 健康检查
        """
        logger.info("stopping_daq_service")
        event_recorder.record_event(
            "service_stopping",
            tags={"component": "edge-daq"},
        )
        
        self.running = False
        
        # 停止采集循环
        if self.plc_reader:
            await self.plc_reader.stop()
        
        # 断开发布器连接
        if self.publisher:
            await self.publisher.disconnect()
        
        # 断开 PLC 连接
        if self.modbus_client:
            await self.modbus_client.disconnect()

        # 停止健康检查服务
        await self._stop_health_server()
        
        logger.info("daq_service_stopped")
        event_recorder.record_event(
            "service_stopped",
            tags={"component": "edge-daq"},
        )

    async def _start_health_server(self):
        """
        启动健康检查 HTTP 服务器
        
        提供 /health 端点供 K8s/Docker 探活使用
        """
        if self.health_runner:
            return

        app = web.Application()

        async def health_handler(request):
            """处理健康检查请求"""
            return web.json_response(self._health_payload())

        app.router.add_get("/health", health_handler)
        self.health_runner = web.AppRunner(app)
        await self.health_runner.setup()
        self.health_site = web.TCPSite(self.health_runner, "0.0.0.0", self.health_port)
        await self.health_site.start()
        logger.info("health_server_started", port=self.health_port)

    async def _stop_health_server(self):
        """停止健康检查服务器"""
        if self.health_runner:
            await self.health_runner.cleanup()
            self.health_runner = None
            self.health_site = None
            logger.info("health_server_stopped")

    def _health_payload(self):
        """
        构建健康检查响应数据
        
        Returns:
            包含服务状态和详细信息的字典
        """
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
    """
    服务主函数
    
    负责：
    1. 创建服务实例
    2. 注册系统信号处理（优雅关闭）
    3. 启动服务并保持运行
    4. 处理异常和清理资源
    """
    service = DAQService()
    
    # 获取事件循环并注册信号处理
    loop = asyncio.get_event_loop()
    
    def signal_handler():
        """处理 SIGTERM/SIGINT 信号，触发优雅关闭"""
        logger.info("received_shutdown_signal")
        asyncio.create_task(service.stop())
    
    # 注册 SIGTERM（Docker stop）和 SIGINT（Ctrl+C）信号
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)
    
    try:
        # 启动采集服务
        await service.start()
        
        # 主循环：保持服务运行直到收到停止信号
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
        # 确保资源被清理
        await service.stop()


if __name__ == "__main__":
    # 运行异步主函数
    asyncio.run(main())

