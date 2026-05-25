"""
边缘计算 API 主入口
工业烘干智能控制系统
"""

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
import structlog
from contextlib import asynccontextmanager
import time

from app.core.config import settings
from app.core.logging import setup_logging
from app.api.v1 import router as api_v1_router
from app.api import realtime as realtime_router
from app.core.database import engine
from app.core.mqtt_client import mqtt_manager
from app.services.event_bus import event_bus
from app.services.health import get_system_readiness
from app.services.outbox_drain import cloud_sync_outbox_drainer
from app.services.ws_stream import ws_stream
from app.services.metrics import refresh_metrics

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

# 设置日志
event_recorder = setup_logging()
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时执行
    logger.info("starting_edge_api", version=settings.VERSION)
    event_recorder.record_event(
        "service_starting",
        fields={"version": settings.VERSION},
        tags={"component": "edge-api"},
    )
    
    # P1-5 phase B：schema 由容器 entrypoint 的 `alembic upgrade head` 接管。
    # alembic 失败 = 容器起不来 = docker 自然 restart 重试，无需 app-level retry。


    # 连接 MQTT & 启动事件总线
    try:
        await mqtt_manager.connect()
    except Exception as exc:
        # 允许启动进入 degraded：MQTT 后续由 paho 自动重连，/ready 会返回 503
        logger.error("mqtt_startup_connect_failed", error=str(exc))
    try:
        await event_bus.start()
    except Exception as exc:
        logger.error("event_bus_start_failed", error=str(exc))
    
    # 启动 MQTT → WS 转发
    try:
        await ws_stream.start()
    except Exception as exc:
        logger.error("ws_stream_start_failed", error=str(exc))
    try:
        await cloud_sync_outbox_drainer.start()
    except Exception as exc:
        logger.error("cloud_sync_outbox_drain_start_failed", error=str(exc))
    
    logger.info("edge_api_started")
    event_recorder.record_event(
        "service_started",
        fields={"version": settings.VERSION},
        tags={"component": "edge-api"},
    )
    
    yield
    
    # 关闭时执行
    logger.info("shutting_down_edge_api")
    event_recorder.record_event(
        "service_stopping",
        fields={"version": settings.VERSION},
        tags={"component": "edge-api"},
    )
    
    # 断开子模块
    await cloud_sync_outbox_drainer.stop()
    await ws_stream.stop()
    await event_bus.stop()
    await mqtt_manager.disconnect()

    # 关闭数据库连接
    await engine.dispose()
    
    logger.info("edge_api_shutdown_complete")
    event_recorder.record_event(
        "service_stopped",
        fields={"version": settings.VERSION},
        tags={"component": "edge-api"},
    )


# 创建 FastAPI 应用
app = FastAPI(
    title="Edge Backend API",
    description="工业烘干智能控制系统边缘计算 API",
    version=settings.VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", settings.API_KEY_HEADER, "X-Request-ID"],
)

# Gzip 压缩中间件
app.add_middleware(GZipMiddleware, minimum_size=1000)


# 请求日志中间件
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """记录所有请求"""
    start_time = time.time()
    
    # 生成请求 ID
    request_id = request.headers.get("X-Request-ID", str(time.time()))
    
    logger.info(
        "request_started",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        client=request.client.host if request.client else None,
    )
    
    try:
        response = await call_next(request)
        
        process_time = time.time() - start_time
        
        logger.info(
            "request_completed",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            process_time=f"{process_time:.3f}s",
        )
        
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time"] = str(process_time)
        
        return response
    
    except Exception as e:
        logger.error(
            "request_failed",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            error=str(e),
        )
        raise


# 全局异常处理
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理器"""
    logger.error(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        error=str(exc),
        exc_info=True,
    )
    event_recorder.record_event(
        "unhandled_exception",
        severity="error",
        fields={
            "path": request.url.path,
            "method": request.method,
        },
        tags={"component": "edge-api"},
    )
    
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Internal server error",
            "message": "An error occurred",
            "timestamp": int(time.time()),
        },
    )


# 注册路由
app.include_router(api_v1_router, prefix="/api/v1")
app.include_router(realtime_router.router, prefix="/api", tags=["Realtime"])


# 根路径
@app.get("/")
async def root():
    """API 根路径"""
    return {
        "success": True,
        "service": "Edge Backend API",
        "version": settings.VERSION,
        "docs": "/docs",
        "timestamp": int(time.time()),
    }


# 健康检查
@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {
        "success": True,
        "status": "healthy",
        "service": "edge-api",
        "version": settings.VERSION,
        "timestamp": int(time.time()),
    }


# 就绪检查
@app.get("/ready")
async def readiness_check():
    """就绪检查端点"""
    report = await get_system_readiness()
    healthy = all(dep.get("status") == "healthy" for dep in report.values())
    payload = {
        "success": healthy,
        "status": "ready" if healthy else "degraded",
        "dependencies": report,
        "timestamp": int(time.time()),
    }
    # 不就绪应返回 503，便于编排系统/监控系统正确判定“不可用”
    # 否则会出现“接口返回 degraded 但 http 仍是 200”的误判
    return JSONResponse(
        status_code=200 if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        content=payload,
    )


@app.get("/metrics")
async def metrics():
    """
    Prometheus 指标端点（P0：90 天游泳可观测性）
    """
    refresh_metrics()
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower(),
    )
