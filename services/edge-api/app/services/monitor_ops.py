"""
Monitor 运维操作客户端。
"""

from __future__ import annotations

from typing import Optional

import httpx

from app.core.config import settings


class MonitorOpsClient:
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or settings.MONITOR_API_BASE).rstrip("/")

    async def restart_service(self, service: str) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.base_url}/ops/self-heal/restart",
                json={"service": service},
            )
            resp.raise_for_status()
            return resp.json()

    async def toggle_self_heal(self, service: str, enabled: bool) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self.base_url}/ops/self-heal/toggle",
                json={"service": service, "enabled": enabled},
            )
            resp.raise_for_status()
            return resp.json()


_monitor_ops_client: MonitorOpsClient | None = None


def get_monitor_ops_client() -> MonitorOpsClient:
    global _monitor_ops_client
    if not _monitor_ops_client:
        _monitor_ops_client = MonitorOpsClient()
    return _monitor_ops_client

