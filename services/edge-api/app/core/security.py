"""
API Key 安全依赖
"""

import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from starlette.websockets import WebSocket

from app.core.config import settings
from app.core.device_identity import DeviceIdentityUnavailable, load_runtime_identity


api_key_scheme = APIKeyHeader(name=settings.API_KEY_HEADER, auto_error=False)
_CLOUD_HEADERS = {
    "device_id": "X-Drycontroller-Cloud-Device",
    "timestamp": "X-Drycontroller-Cloud-Timestamp",
    "nonce": "X-Drycontroller-Cloud-Nonce",
    "signature": "X-Drycontroller-Cloud-Signature",
}
_NONCE = re.compile(r"^[a-f0-9]{32}$")
_CLOUD_NONCE_LOCK = asyncio.Lock()


def _validate_key(api_key: str | None, required: str) -> str:
    if not settings.api_security_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API key security is not configured",
        )
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
            headers={"WWW-Authenticate": "API-Key"},
        )

    admin_keys = set(settings.ADMIN_API_KEYS)
    if required == "admin":
        allowed = admin_keys
    else:
        allowed = admin_keys.union(settings.CONTROL_API_KEYS)

    if not any(hmac.compare_digest(api_key, k) for k in allowed):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key not permitted",
        )
    return api_key


def _cloud_public_key() -> Ed25519PublicKey:
    path = Path(settings.CLOUD_IDENTITY_PUBLIC_KEY_FILE)
    try:
        metadata = path.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > 1024
            or metadata.st_mode & 0o022
            or (os.geteuid() == 0 and metadata.st_uid != 0)
        ):
            raise ValueError("invalid Cloud trust root metadata")
        raw = base64.b64decode(path.read_text(encoding="ascii").strip(), validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cloud request trust root is unavailable",
        ) from exc


def _canonical_cloud_request(
    *,
    device_id: str,
    method: str,
    path: str,
    query: list[tuple[str, str]],
    body: object | None,
    timestamp: str,
    nonce: str,
) -> bytes:
    canonical_body = (
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if body is not None
        else b""
    )
    payload = {
        "audience": "drycontroller-edge-api",
        "body_sha256": hashlib.sha256(canonical_body).hexdigest(),
        "device_id": device_id,
        "method": method.upper(),
        "nonce": nonce,
        "path": path,
        "query": sorted([list(item) for item in query]),
        "schema": "drycontroller.cloud-edge-request.v1",
        "timestamp": timestamp,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validate_cloud_nonce_store(path: Path) -> None:
    parent_metadata = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_mode & 0o077
        or (os.geteuid() == 0 and parent_metadata.st_uid != 0)
    ):
        raise OSError("invalid Cloud nonce store directory")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o077
        or (os.geteuid() == 0 and metadata.st_uid != 0)
    ):
        raise OSError("invalid Cloud nonce store")


def _reserve_cloud_nonce(*, nonce: str, now: int, expires_at: int) -> bool:
    path = Path(settings.CLOUD_REQUEST_NONCE_DB)
    _validate_cloud_nonce_store(path)
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    try:
        path.chmod(0o600)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS cloud_nonces "
            "(nonce TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)"
        )
        connection.execute("DELETE FROM cloud_nonces WHERE expires_at < ?", (now,))
        try:
            connection.execute(
                "INSERT INTO cloud_nonces (nonce, expires_at) VALUES (?, ?)",
                (nonce, expires_at),
            )
        except sqlite3.IntegrityError:
            connection.rollback()
            return False
        connection.commit()
        return True
    finally:
        connection.close()


async def _validate_cloud_request(request: Request) -> str | None:
    values = {name: request.headers.get(header) for name, header in _CLOUD_HEADERS.items()}
    if not any(values.values()):
        return None
    if not all(values.values()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Cloud request rejected")
    device_id = str(values["device_id"])
    timestamp_text = str(values["timestamp"])
    nonce = str(values["nonce"])
    try:
        timestamp_value = int(timestamp_text)
        signature = base64.b64decode(str(values["signature"]), validate=True)
        identity = load_runtime_identity(require_root_owner=False)
    except (ValueError, DeviceIdentityUnavailable):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Cloud request rejected") from None
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    if (
        device_id != identity.get("device_id")
        or _NONCE.fullmatch(nonce) is None
        or abs(now - timestamp_value) > 30
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Cloud request rejected")
    raw_body = await request.body()
    try:
        body = json.loads(raw_body) if raw_body else None
        canonical = _canonical_cloud_request(
            device_id=device_id,
            method=request.method,
            path=request.url.path,
            query=list(request.query_params.multi_items()),
            body=body,
            timestamp=timestamp_text,
            nonce=nonce,
        )
        _cloud_public_key().verify(signature, canonical)
    except (InvalidSignature, json.JSONDecodeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Cloud request rejected") from None
    try:
        async with _CLOUD_NONCE_LOCK:
            reserved = _reserve_cloud_nonce(
                nonce=nonce,
                now=now,
                expires_at=timestamp_value + 30,
            )
    except (OSError, sqlite3.Error):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cloud request replay protection is unavailable",
        ) from None
    if not reserved:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cloud request replay rejected")
    return f"cloud:{device_id}"


async def _require_role(request: Request, api_key: str | None, *, role: str) -> str:
    cloud_identity = await _validate_cloud_request(request)
    if cloud_identity is not None:
        return cloud_identity
    return _validate_key(api_key, required=role)


def is_control_api_key_allowed(api_key: str | None) -> bool:
    if not settings.api_security_enabled or not api_key:
        return False

    allowed = set(settings.ADMIN_API_KEYS).union(settings.CONTROL_API_KEYS)
    return any(hmac.compare_digest(api_key, key) for key in allowed)


def is_readonly_source_allowed(host: str | None) -> bool:
    clean_host = (host or "").strip()
    if not clean_host:
        return False

    try:
        client_ip = ipaddress.ip_address(clean_host)
    except ValueError:
        client_ip = None

    for item in settings.READONLY_WS_ALLOWLIST:
        entry = str(item).strip()
        if not entry:
            continue
        if clean_host == entry:
            return True
        if client_ip is None:
            continue
        try:
            if client_ip in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


async def require_readonly_http_source(
    request: Request,
    api_key: str | None = Depends(api_key_scheme),
) -> None:
    host = request.client.host if request.client else ""
    if is_readonly_source_allowed(host):
        return
    if await _validate_cloud_request(request) is not None:
        return
    if api_key:
        _validate_key(api_key, required="control")
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Read-only data access is not allowed from this source",
    )


async def require_readonly_ws_source(websocket: WebSocket) -> bool:
    host = websocket.client.host if websocket.client else ""
    if is_readonly_source_allowed(host):
        return True
    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
    return False


async def require_control_api_key(
    request: Request,
    api_key: str | None = Depends(api_key_scheme),
) -> str:
    """
    需要控制权限（含 admin）
    """
    return await _require_role(request, api_key, role="control")


async def require_admin_api_key(
    request: Request,
    api_key: str | None = Depends(api_key_scheme),
) -> str:
    """
    需要管理员权限
    """
    return await _require_role(request, api_key, role="admin")
