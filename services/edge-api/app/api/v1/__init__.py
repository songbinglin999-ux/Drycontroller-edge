"""
API v1 路由
"""

from fastapi import APIRouter

from app.api.v1 import ai, alarms, control, data, ops, system

router = APIRouter()

# 注册子路由
router.include_router(data.router, prefix="/data", tags=["Data"])
router.include_router(control.router, prefix="/control", tags=["Control"])
router.include_router(ai.router, prefix="/ai", tags=["AI"])
router.include_router(alarms.router, prefix="/alarms", tags=["Alarms"])
router.include_router(system.router, prefix="/system", tags=["System"])
router.include_router(ops.router, prefix="/ops", tags=["Ops"])

