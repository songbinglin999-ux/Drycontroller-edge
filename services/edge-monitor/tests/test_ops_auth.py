"""
edge-monitor /ops 高危接口鉴权单测（P0）

覆盖 require_ops_api_key 的三类错误码：
- 未配置任何 key：503
- 缺失 key：401
- key 不匹配：403
"""

import os
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

from monitor.main import require_ops_api_key  # noqa: E402


def _client() -> TestClient:
    app = FastAPI()

    @app.get("/ops/ping")
    async def ping(_: str = Depends(require_ops_api_key)):
        return {"ok": True}

    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    # 避免外部环境变量影响测试
    for k in ["MONITOR_OPS_API_KEYS", "ADMIN_API_KEYS"]:
        monkeypatch.delenv(k, raising=False)
    yield


def test_ops_auth_not_configured_503():
    c = _client()
    resp = c.get("/ops/ping")
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"].lower()


def test_ops_auth_missing_key_401(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MONITOR_OPS_API_KEYS", "k1")
    c = _client()
    resp = c.get("/ops/ping")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Missing API key"


def test_ops_auth_wrong_key_403(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MONITOR_OPS_API_KEYS", "k1")
    c = _client()
    resp = c.get("/ops/ping", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 403


def test_ops_auth_accepts_admin_keys_fallback(monkeypatch: pytest.MonkeyPatch):
    # 未设置 MONITOR_OPS_API_KEYS 时，允许复用 ADMIN_API_KEYS
    monkeypatch.setenv("ADMIN_API_KEYS", "admin-1,admin-2")
    c = _client()
    resp = c.get("/ops/ping", headers={"X-API-Key": "admin-2"})
    assert resp.status_code == 200


