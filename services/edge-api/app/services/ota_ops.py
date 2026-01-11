"""
OTA Agent 运维操作客户端（由 edge-api 代理调用）。
"""

from __future__ import annotations

from typing import Optional

import httpx

from app.core.config import settings


class OtaOpsClient:
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or settings.OTA_AGENT_API_BASE).rstrip("/")

    def _headers(self, api_key: str | None) -> dict:
        if not api_key:
            return {}
        return {settings.API_KEY_HEADER: api_key}

    async def start(self, *, api_key: str | None, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.base_url}/ota/start",
                json=payload,
                headers=self._headers(api_key),
            )
            resp.raise_for_status()
            return resp.json()

    async def get_operation(self, operation_id: str, *, api_key: str | None) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.base_url}/ota/operations/{operation_id}",
                headers=self._headers(api_key),
            )
            resp.raise_for_status()
            return resp.json()

    async def get_logs(self, operation_id: str, *, api_key: str | None, lines: int = 200) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.base_url}/ota/operations/{operation_id}/logs",
                params={"lines": lines},
                headers=self._headers(api_key),
            )
            resp.raise_for_status()
            return resp.json()


_ota_ops_client: OtaOpsClient | None = None


def get_ota_ops_client() -> OtaOpsClient:
    global _ota_ops_client
    if not _ota_ops_client:
        _ota_ops_client = OtaOpsClient()
    return _ota_ops_client


