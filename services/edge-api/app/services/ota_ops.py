"""Fail-closed client for the root-owned OTA operation helper."""

from __future__ import annotations

import re
import stat
from pathlib import Path

import httpx
from fastapi import HTTPException


OTA_HELPER_SOCKET = Path("/run/drycontroller-ota/helper.sock")
OTA_HELPER_BASE_URL = "http://drycontroller-ota-helper.invalid"
OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")


def _socket_metadata_error(metadata: object) -> str | None:
    mode = int(getattr(metadata, "st_mode"))
    if not stat.S_ISSOCK(mode):
        return "not_unix_socket"
    if int(getattr(metadata, "st_uid")) != 0:
        return "not_root_owned"
    if int(getattr(metadata, "st_gid")) != 0:
        return "unexpected_group"
    if stat.S_IMODE(mode) != 0o660:
        return "unexpected_mode"
    if int(getattr(metadata, "st_nlink")) != 1:
        return "unexpected_link_count"
    return None


def _validated_helper_socket() -> Path:
    try:
        metadata = OTA_HELPER_SOCKET.lstat()
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "ota_helper_unavailable", "reason": "socket_missing"},
        ) from exc
    reason = _socket_metadata_error(metadata)
    if reason:
        raise HTTPException(
            status_code=503,
            detail={"error": "ota_helper_unavailable", "reason": reason},
        )
    return OTA_HELPER_SOCKET


def _validated_operation_id(value: str) -> str:
    if OPERATION_ID.fullmatch(value) is None:
        raise HTTPException(status_code=422, detail="invalid OTA operation id")
    return value


class OtaOpsClient:
    async def _request(self, method: str, path: str, *, timeout: float, **kwargs: object) -> dict:
        socket_path = _validated_helper_socket()
        transport = httpx.AsyncHTTPTransport(uds=str(socket_path), retries=0)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url=OTA_HELPER_BASE_URL,
                timeout=timeout,
                trust_env=False,
            ) as client:
                response = await client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=503,
                detail={"error": "ota_helper_unavailable", "reason": "connect_failed"},
            ) from exc
        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = {"error": "ota_helper_invalid_error_response"}
            raise HTTPException(status_code=response.status_code, detail=detail)
        try:
            payload = response.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail={"error": "ota_helper_invalid_response"},
            ) from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=502, detail={"error": "ota_helper_invalid_response"})
        return payload

    async def ready(self) -> dict:
        return await self._request("GET", "/ready", timeout=5)

    async def start(self, operation_id: str) -> dict:
        operation_id = _validated_operation_id(operation_id)
        return await self._request(
            "POST",
            "/ota/start",
            timeout=15,
            json={"operation_id": operation_id},
        )

    async def get_operation(self, operation_id: str) -> dict:
        operation_id = _validated_operation_id(operation_id)
        return await self._request("GET", f"/ota/operations/{operation_id}", timeout=10)

    async def get_logs(self, operation_id: str) -> dict:
        operation_id = _validated_operation_id(operation_id)
        return await self._request("GET", f"/ota/operations/{operation_id}/logs", timeout=10)


_ota_ops_client: OtaOpsClient | None = None


def get_ota_ops_client() -> OtaOpsClient:
    global _ota_ops_client
    if not _ota_ops_client:
        _ota_ops_client = OtaOpsClient()
    return _ota_ops_client
