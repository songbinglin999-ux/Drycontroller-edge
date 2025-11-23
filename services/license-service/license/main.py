"""
授权服务
"""

from fastapi import FastAPI
import structlog

logger = structlog.get_logger(__name__)

app = FastAPI(title="License Service")

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/verify")
async def verify_license():
    # TODO: 实现加密狗验证逻辑
    return {"success": True, "valid": True}

