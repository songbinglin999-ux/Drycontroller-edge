"""
AI 软测量服务 - 含水率预测

本服务实现基于 AI 模型的在线含水率软测量功能，主要用于：
- 实时预测烘干后物料的含水率（替代离线化验）
- 为控制优化提供实时反馈信号
- 减少人工采样和化验的工作量

工作原理：
1. 订阅实时采集数据（温度、链速、风量等）
2. 使用训练好的模型进行推理
3. 将预测结果发布到 MQTT 供其他服务使用

架构流程：
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  MQTT 订阅   │ -> │ SoftSensorModel │ -> │  MQTT 发布   │
│ (实时数据)    │    │  (模型推理)    │    │ (预测结果)   │
└──────────────┘    └──────────────┘    └──────────────┘

环境变量：
- MQTT_BROKER/MQTT_PORT: MQTT 服务器配置
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
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from common.logging import setup_logging
from src.infer import SoftSensorModel
from src.mqtt_handler import MQTTHandler

event_recorder = setup_logging("ai-softsensor")
logger = structlog.get_logger(__name__)


class SoftSensorService:
    """
    软测量服务主类
    
    协调模型加载、MQTT 通信和服务生命周期管理。
    
    Attributes:
        running: 服务运行状态
        model: 软测量模型实例
        mqtt_handler: MQTT 消息处理器
        config: 服务配置
    """
    
    def __init__(self):
        """初始化软测量服务"""
        self.running = False
        self.model = None
        self.mqtt_handler = None
        self.config = None
        # 健康检查服务器
        self.health_runner = None
        self.health_site = None
        self.health_port = int(os.getenv("HEALTH_PORT", "8080"))
    
    async def load_config(self):
        """
        加载 AI 配置文件
        
        配置文件定义了模型路径、输入特征、输出配置等
        
        Returns:
            加载的配置字典
        """
        config_path = Path("/app/configs/ai-config.yaml")
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)
        
        logger.info("config_loaded")
        return self.config
    
    async def start(self):
        """
        启动软测量服务
        
        启动流程：
        1. 加载配置文件
        2. 启动健康检查服务
        3. 初始化并加载 AI 模型
        4. 连接 MQTT 并开始订阅数据
        """
        logger.info("starting_softsensor_service")
        event_recorder.record_event(
            "service_starting",
            tags={"component": "ai-softsensor"},
        )
        
        await self.load_config()
        await self._start_health_server()
        
        # 初始化并加载模型
        model_config = self.config.get("softsensor", {})
        self.model = SoftSensorModel(model_config)
        await self.model.load()
        
        # 初始化 MQTT 处理器并连接
        self.mqtt_handler = MQTTHandler(self.model, self.config)
        await self.mqtt_handler.connect()
        
        self.running = True
        logger.info("softsensor_service_started")
        event_recorder.record_event(
            "service_started",
            tags={"component": "ai-softsensor"},
        )
    
    async def stop(self):
        """
        停止软测量服务
        
        按顺序清理资源：MQTT -> 健康检查服务
        """
        logger.info("stopping_softsensor_service")
        event_recorder.record_event(
            "service_stopping",
            tags={"component": "ai-softsensor"},
        )
        
        self.running = False
        
        # 断开 MQTT 连接
        if self.mqtt_handler:
            await self.mqtt_handler.disconnect()
            self.mqtt_handler = None

        # 停止健康检查服务
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

        async def ready_handler(request):
            payload = self._ready_payload()
            status = 200 if payload.get("ready") else 503
            return web.json_response(payload, status=status)

        async def metrics_handler(request):
            data = generate_latest()
            # aiohttp 不允许在 content_type 参数中包含 charset；prometheus_client 常量带 charset
            return web.Response(body=data, headers={"Content-Type": CONTENT_TYPE_LATEST})

        app.router.add_get("/health", health_handler)
        app.router.add_get("/ready", ready_handler)
        app.router.add_get("/metrics", metrics_handler)
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
        dq = None
        if self.mqtt_handler:
            try:
                dq = self.mqtt_handler.dq_snapshot()
            except Exception:
                dq = None
        return {
            "success": True,
            "service": "ai-softsensor",
            "status": "running" if self.running else "starting",
            "timestamp": int(time.time()),
            "details": {
                "model_loaded": self.model is not None,
                "mqtt_connected": bool(self.mqtt_handler and self.mqtt_handler.connected),
                "dq": dq,
            },
        }

    def _ready_payload(self):
        model_loaded = self.model is not None
        mqtt_ready = bool(self.mqtt_handler and self.mqtt_handler.connected)
        dq_ready = bool(self.mqtt_handler and self.mqtt_handler.is_dq_ready()) if self.mqtt_handler else False
        ready = bool(self.running and model_loaded and mqtt_ready and dq_ready)
        reason = None
        hint = None
        if not self.running:
            reason = "SERVICE_STARTING"
        elif not model_loaded:
            reason = "MODEL_NOT_READY"
            hint = "模型未加载：检查 AI_MODEL_PATH / 模型文件是否存在。"
        elif not mqtt_ready:
            reason = "MQTT_OFFLINE"
            hint = "MQTT 未连接：检查 mosquitto 以及账号密码。"
        elif not dq_ready:
            reason = "DQ_NOT_READY"
            hint = "数据链路未就绪：通常是 edge-daq/PLC 侧没有稳定产出数据（例如 PLC 离线导致 edge-daq /ready=PLC_OFFLINE）。"
        return {
            "ready": ready,
            "service": "ai-softsensor",
            "timestamp": int(time.time()),
            "reason": reason,
            "hint": hint,
            "details": {
                "running": bool(self.running),
                "model_loaded": model_loaded,
                "mqtt_connected": mqtt_ready,
                "dq_ready": dq_ready,
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

