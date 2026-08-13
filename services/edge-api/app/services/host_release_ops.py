"""Fail-closed client for the immutable root-owned HostRelease runner."""

from __future__ import annotations

import re
import stat
from pathlib import Path

import httpx
from fastapi import HTTPException


HOST_RUNNER_SOCKET = Path("/run/drycontroller-host-release/runner.sock")
HOST_RUNNER_BASE_URL = "http://drycontroller-host-release.invalid"
OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")


def _validated_socket() -> Path:
    try:
        metadata = HOST_RUNNER_SOCKET.lstat()
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "host_release_runner_unavailable", "reason": "socket_missing"},
        ) from exc
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o660
        or metadata.st_nlink != 1
    ):
        raise HTTPException(
            status_code=503,
            detail={"error": "host_release_runner_unavailable", "reason": "socket_untrusted"},
        )
    return HOST_RUNNER_SOCKET


def _operation_id(value: str) -> str:
    if OPERATION_ID.fullmatch(value) is None:
        raise HTTPException(status_code=422, detail="invalid HostRelease operation id")
    return value


class HostReleaseOpsClient:
    async def _request(self, method: str, path: str, *, timeout: float, **kwargs: object) -> dict:
        transport = httpx.AsyncHTTPTransport(uds=str(_validated_socket()), retries=0)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url=HOST_RUNNER_BASE_URL,
                timeout=timeout,
                trust_env=False,
            ) as client:
                response = await client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=503,
                detail={"error": "host_release_runner_unavailable", "reason": "connect_failed"},
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail={"error": "host_release_runner_invalid_response"}) from exc
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=payload)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=502, detail={"error": "host_release_runner_invalid_response"})
        return payload

    async def start(self, operation_id: str) -> dict:
        operation_id = _operation_id(operation_id)
        return await self._request(
            "POST",
            "/host-release/start",
            timeout=15,
            json={"operation_id": operation_id},
        )

    async def get_operation(self, operation_id: str) -> dict:
        operation_id = _operation_id(operation_id)
        return await self._request("GET", f"/host-release/operations/{operation_id}", timeout=10)

    async def get_logs(self, operation_id: str) -> dict:
        operation_id = _operation_id(operation_id)
        return await self._request("GET", f"/host-release/operations/{operation_id}/logs", timeout=10)


_client: HostReleaseOpsClient | None = None


def get_host_release_ops_client() -> HostReleaseOpsClient:
    global _client
    if _client is None:
        _client = HostReleaseOpsClient()
    return _client
