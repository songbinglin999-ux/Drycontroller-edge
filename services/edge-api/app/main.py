"""
边缘计算 API 主入口
工业烘干智能控制系统
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
import structlog
from contextlib import asynccontextmanager
import time

from app.core.config import settings
from app.core.logging import setup_logging
from app.api.v1 import router as api_v1_router
from app.core.database import engine, Base
from app.core.mqtt_client import mqtt_manager

# 设置日志
setup_logging()
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时执行
    logger.info("starting_edge_api", version=settings.VERSION)
    
    # 创建数据库表
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # 连接 MQTT
    await mqtt_manager.connect()
    
    logger.info("edge_api_started")
    
    yield
    
    # 关闭时执行
    logger.info("shutting_down_edge_api")
    
    # 断开 MQTT
    await mqtt_manager.disconnect()
    
    # 关闭数据库连接
    await engine.dispose()
    
    logger.info("edge_api_shutdown_complete")


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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
    
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Internal server error",
            "message": str(exc) if settings.DEBUG else "An error occurred",
            "timestamp": int(time.time()),
        },
    )


# 注册路由
app.include_router(api_v1_router, prefix="/api/v1")


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
    # TODO: 检查数据库、MQTT 等依赖服务的连接状态
    
    return {
        "success": True,
        "status": "ready",
        "dependencies": {
            "database": "connected",
            "mqtt": "connected",
            "influxdb": "connected",
        },
        "timestamp": int(time.time()),
    }


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower(),
    )

