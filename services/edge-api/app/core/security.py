"""
API Key 安全依赖
"""

import hmac
import ipaddress

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from starlette.websockets import WebSocket

from app.core.config import settings


api_key_scheme = APIKeyHeader(name=settings.API_KEY_HEADER, auto_error=False)


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


async def require_control_api_key(api_key: str = Depends(api_key_scheme)) -> str:
    """
    需要控制权限（含 admin）
    """
    return _validate_key(api_key, required="control")


async def require_admin_api_key(api_key: str = Depends(api_key_scheme)) -> str:
    """
    需要管理员权限
    """
    return _validate_key(api_key, required="admin")
