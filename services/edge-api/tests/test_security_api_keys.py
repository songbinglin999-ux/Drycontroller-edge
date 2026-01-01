"""
edge-api API key 鉴权单测（P0）

目标：
- 未配置任何 key 时：返回 503（避免默认裸奔）
- 缺失 key：401
- key 不匹配：403
- control/admin 权限边界：control 接受 control+admin；admin 只接受 admin
"""

import sys
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]
for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

from app.core.config import settings  # noqa: E402
from app.core.security import require_admin_api_key, require_control_api_key  # noqa: E402


def _build_app() -> TestClient:
    app = FastAPI()

    @app.get("/control")
    async def control(_: str = Depends(require_control_api_key)):
        return {"ok": True}

    @app.get("/admin")
    async def admin(_: str = Depends(require_admin_api_key)):
        return {"ok": True}

    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_keys():
    old_control = list(settings.CONTROL_API_KEYS)
    old_admin = list(settings.ADMIN_API_KEYS)
    old_header = settings.API_KEY_HEADER
    try:
        # 用默认 header，避免被环境变量污染
        settings.API_KEY_HEADER = "X-API-Key"
        settings.CONTROL_API_KEYS = []
        settings.ADMIN_API_KEYS = []
        yield
    finally:
        settings.CONTROL_API_KEYS = old_control
        settings.ADMIN_API_KEYS = old_admin
        settings.API_KEY_HEADER = old_header


def test_security_not_configured_returns_503():
    client = _build_app()
    resp = client.get("/admin")
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"].lower()


def test_missing_api_key_returns_401():
    settings.ADMIN_API_KEYS = ["admin-1"]
    client = _build_app()
    resp = client.get("/admin")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Missing API key"


def test_wrong_api_key_returns_403():
    settings.ADMIN_API_KEYS = ["admin-1"]
    client = _build_app()
    resp = client.get("/admin", headers={settings.API_KEY_HEADER: "wrong"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "API key not permitted"


def test_control_endpoint_accepts_control_and_admin_keys():
    settings.CONTROL_API_KEYS = ["ctl-1"]
    settings.ADMIN_API_KEYS = ["admin-1"]
    client = _build_app()

    resp = client.get("/control", headers={settings.API_KEY_HEADER: "ctl-1"})
    assert resp.status_code == 200

    resp2 = client.get("/control", headers={settings.API_KEY_HEADER: "admin-1"})
    assert resp2.status_code == 200


def test_admin_endpoint_rejects_control_key():
    settings.CONTROL_API_KEYS = ["ctl-1"]
    settings.ADMIN_API_KEYS = ["admin-1"]
    client = _build_app()

    resp = client.get("/admin", headers={settings.API_KEY_HEADER: "ctl-1"})
    assert resp.status_code == 403


