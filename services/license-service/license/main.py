"""
授权服务
"""

from fastapi import FastAPI
import structlog

from common.logging import setup_logging

event_recorder = setup_logging("license-service")
logger = structlog.get_logger(__name__)

app = FastAPI(title="License Service")


@app.on_event("startup")
async def startup():
    logger.info("license_service_starting")
    event_recorder.record_event(
        "service_starting",
        tags={"component": "license-service"},
    )
    logger.info("license_service_started")
    event_recorder.record_event(
        "service_started",
        tags={"component": "license-service"},
    )


@app.on_event("shutdown")
async def shutdown():
    logger.info("license_service_stopping")
    event_recorder.record_event(
        "service_stopping",
        tags={"component": "license-service"},
    )
    logger.info("license_service_stopped")
    event_recorder.record_event(
        "service_stopped",
        tags={"component": "license-service"},
    )

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/verify")
async def verify_license():
    # TODO: 实现加密狗验证逻辑
    result = {"success": True, "valid": True}
    event_recorder.record_event(
        "license_verified",
        tags={"component": "license-service"},
        fields={"valid": result["valid"]},
    )
    return result

