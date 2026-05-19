"""
API Key 安全依赖
"""

import hmac

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader

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


