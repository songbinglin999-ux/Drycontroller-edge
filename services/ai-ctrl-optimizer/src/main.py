"""
AI 控制优化服务
"""

import asyncio
import os
import signal
import time

import structlog
from aiohttp import web
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from common.logging import setup_logging

event_recorder = setup_logging("ai-ctrl-optimizer")
logger = structlog.get_logger(__name__)


class OptimizerService:
    def __init__(self):
        self.running = False
        self.health_runner = None
        self.health_site = None
        self.health_port = int(os.getenv("HEALTH_PORT", "8080"))
        self._loop_task = None
    
    async def start(self):
        logger.info("starting_ai_ctrl_optimizer")
        event_recorder.record_event(
            "service_starting",
            tags={"component": "ai-ctrl-optimizer"},
        )
        await self._start_health_server()
        self.running = True
        self._loop_task = asyncio.create_task(self._optimizer_loop())
        logger.info("ai_ctrl_optimizer_started")
        event_recorder.record_event(
            "service_started",
            tags={"component": "ai-ctrl-optimizer"},
        )
    
    async def stop(self):
        if not self.running and not self.health_runner:
            return
        
        logger.info("stopping_ai_ctrl_optimizer")
        event_recorder.record_event(
            "service_stopping",
            tags={"component": "ai-ctrl-optimizer"},
        )
        self.running = False
        
        if self._loop_task:
            await self._loop_task
            self._loop_task = None
        
        await self._stop_health_server()
        logger.info("ai_ctrl_optimizer_stopped")
        event_recorder.record_event(
            "service_stopped",
            tags={"component": "ai-ctrl-optimizer"},
        )
    
    async def _optimizer_loop(self):
        # TODO: 实现控制优化逻辑
        while self.running:
            await asyncio.sleep(60)
    
    async def _start_health_server(self):
        if self.health_runner:
            return
        
        app = web.Application()

        async def health_handler(request):
            return web.json_response(self._health_payload())

        async def ready_handler(request):
            payload = {
                "ready": bool(self.running),
                "service": "ai-ctrl-optimizer",
                "timestamp": int(time.time()),
            }
            return web.json_response(payload, status=200 if payload["ready"] else 503)

        async def metrics_handler(request):
            # Prometheus exposition format. aiohttp 不允许在 content_type 参数中
            # 包含 charset；prometheus_client 常量带 charset，所以用 headers 传。
            return web.Response(body=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})

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
        return {
            "success": True,
            "service": "ai-ctrl-optimizer",
            "status": "running" if self.running else "starting",
            "timestamp": int(time.time()),
        }


async def main():
    service = OptimizerService()
    loop = asyncio.get_event_loop()
    
    def signal_handler():
        asyncio.create_task(service.stop())
    
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)
    
    try:
        await service.start()
        while service.running:
            await asyncio.sleep(1)
    except Exception as exc:  # noqa: BLE001 - 故意宽捕：顶层服务韧性边界，未知异常也须记录并 graceful stop
        logger.error("optimizer_error", error=str(exc), exc_info=True)
        event_recorder.record_event(
            "service_error",
            severity="error",
            fields={"error": str(exc)},
            tags={"component": "ai-ctrl-optimizer"},
        )
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())
