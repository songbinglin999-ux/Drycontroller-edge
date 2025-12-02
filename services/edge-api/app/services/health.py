"""
系统依赖健康检查工具
"""

from __future__ import annotations

import asyncio
from typing import Dict

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine
from app.core.mqtt_client import mqtt_manager
from app.services.event_bus import event_bus
from app.services.ws_stream import ws_stream


async def _check_postgres() -> Dict[str, str]:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "healthy"}
    except Exception as exc:  # pragma: no cover - 依赖外部服务
        return {"status": "down", "detail": str(exc)}


async def _check_influx() -> Dict[str, str]:
    url = f"{settings.influxdb_url}/health"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(url)
        if response.status_code == 200:
            payload = response.json() if response.content else {}
            status = payload.get("status", "").lower()
            if status in {"pass", "ok", "healthy"}:
                return {"status": "healthy"}
        return {
            "status": "degraded",
            "detail": f"http {response.status_code}",
        }
    except Exception as exc:  # pragma: no cover - 依赖外部服务
        return {"status": "down", "detail": str(exc)}


async def _check_mqtt() -> Dict[str, str]:
    return {
        "status": "healthy" if mqtt_manager.connected else "down",
        "detail": None if mqtt_manager.connected else "client_not_connected",
    }


async def _check_event_bus() -> Dict[str, str]:
    try:
        stats = event_bus.get_stats()
        started = stats.get("started", False)
        return {
            "status": "healthy" if started else "down",
            "subscriptions": stats.get("subscriptions", []),
        }
    except Exception as exc:  # pragma: no cover - 理论上不会
        return {"status": "down", "detail": str(exc)}


async def _check_ws_stream() -> Dict[str, str]:
    return {
        "status": "healthy" if ws_stream.started else "down",
    }


async def get_system_readiness() -> Dict[str, Dict[str, str]]:
    """
    并发执行所有依赖检查，返回统一报告
    """

    results = await asyncio.gather(
        _check_postgres(),
        _check_influx(),
        _check_mqtt(),
        _check_event_bus(),
        _check_ws_stream(),
    )
    return {
        "database": results[0],
        "influxdb": results[1],
        "mqtt": results[2],
        "event_bus": results[3],
        "ws_stream": results[4],
    }


