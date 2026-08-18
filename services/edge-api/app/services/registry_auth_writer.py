"""Fail-closed client for the root-owned registry auth writer."""

from __future__ import annotations

import stat
from pathlib import Path

import httpx
from fastapi import HTTPException


WRITER_SOCKET = Path("/run/drycontroller-registry-auth/writer.sock")
WRITER_BASE_URL = "http://drycontroller-registry-auth.invalid"


def _validated_socket() -> Path:
    try:
        metadata = WRITER_SOCKET.lstat()
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "registry_auth_writer_unavailable", "reason": "socket_missing"},
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
            detail={"error": "registry_auth_writer_unavailable", "reason": "socket_untrusted"},
        )
    return WRITER_SOCKET


class RegistryAuthWriterClient:
    async def _request(self, method: str, path: str, *, payload: dict[str, object] | None = None) -> dict:
        transport = httpx.AsyncHTTPTransport(uds=str(_validated_socket()), retries=0)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url=WRITER_BASE_URL,
                timeout=10,
                trust_env=False,
            ) as client:
                response = await client.request(method, path, json=payload)
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=503,
                detail={"error": "registry_auth_writer_unavailable", "reason": "connect_failed"},
            ) from exc
        try:
            result = response.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail={"error": "registry_auth_writer_invalid_response"},
            ) from exc
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=result)
        if not isinstance(result, dict) or result.get("success") is not True:
            raise HTTPException(
                status_code=502,
                detail={"error": "registry_auth_writer_invalid_response"},
            )
        credential = result.get("credential")
        if not isinstance(credential, dict) or "secret" in credential:
            raise HTTPException(
                status_code=502,
                detail={"error": "registry_auth_writer_invalid_response"},
            )
        return credential

    async def install(self, payload: dict[str, object]) -> dict:
        return await self._request("POST", "/registry-auth/install", payload=payload)

    async def status(self) -> dict:
        return await self._request("GET", "/registry-auth/status")


_client: RegistryAuthWriterClient | None = None


def get_registry_auth_writer_client() -> RegistryAuthWriterClient:
    global _client
    if _client is None:
        _client = RegistryAuthWriterClient()
    return _client
