"""
edge-api API key 鉴权单测（P0）

目标：
- 未配置任何 key 时：返回 503（避免默认裸奔）
- 缺失 key：401
- key 不匹配：403
- control/admin 权限边界：control 接受 control+admin；admin 只接受 admin
"""

import asyncio
import base64
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import pytest
import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
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


def _create_app() -> FastAPI:
    app = FastAPI()

    @app.get("/control")
    async def control(_: str = Depends(require_control_api_key)):
        return {"ok": True}

    @app.get("/admin")
    async def admin(_: str = Depends(require_admin_api_key)):
        return {"ok": True}

    return app


def _build_app() -> TestClient:
    return TestClient(_create_app())


@pytest.fixture(autouse=True)
def _reset_keys(tmp_path):
    old_control = list(settings.CONTROL_API_KEYS)
    old_admin = list(settings.ADMIN_API_KEYS)
    old_header = settings.API_KEY_HEADER
    old_cloud_key = settings.CLOUD_IDENTITY_PUBLIC_KEY_FILE
    old_identity_path = settings.DEVICE_IDENTITY_PATH
    old_nonce_db = settings.CLOUD_REQUEST_NONCE_DB
    nonce_dir = tmp_path / "cloud-nonces"
    nonce_dir.mkdir(mode=0o700)
    from app.core import security

    old_nonce_lock = security._CLOUD_NONCE_LOCK
    try:
        # 用默认 header，避免被环境变量污染
        settings.API_KEY_HEADER = "X-API-Key"
        settings.CONTROL_API_KEYS = []
        settings.ADMIN_API_KEYS = []
        settings.CLOUD_REQUEST_NONCE_DB = str(nonce_dir / "nonces.sqlite3")
        security._CLOUD_NONCE_LOCK = asyncio.Lock()
        yield
    finally:
        settings.CONTROL_API_KEYS = old_control
        settings.ADMIN_API_KEYS = old_admin
        settings.API_KEY_HEADER = old_header
        settings.CLOUD_IDENTITY_PUBLIC_KEY_FILE = old_cloud_key
        settings.DEVICE_IDENTITY_PATH = old_identity_path
        settings.CLOUD_REQUEST_NONCE_DB = old_nonce_db
        security._CLOUD_NONCE_LOCK = old_nonce_lock


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


def test_cloud_signature_is_device_path_body_bound_and_replay_safe(tmp_path, monkeypatch):
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public_path = tmp_path / "cloud.pub"
    public_path.write_text(
        base64.b64encode(private_key.public_key().public_bytes_raw()).decode("ascii"),
        encoding="ascii",
    )
    public_path.chmod(0o644)
    identity_path = tmp_path / "device.json"
    identity = {
        "schema": "drycontroller.device-identity.v1",
        "device_id": "EDGE-ABCDEF123456",
        "tenant_id": "tenant-a",
        "line_id": "LINE-A",
        "node_name": "edge-abcdef123456",
        "identity_fingerprint": "ab" * 32,
        "runtime_credential": "runtime-secret",
        "runtime_credential_hash": hashlib.sha256(b"runtime-secret").hexdigest(),
        "credential_expires_at": (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        ).isoformat(),
    }
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    identity_path.chmod(0o600)
    settings.CLOUD_IDENTITY_PUBLIC_KEY_FILE = str(public_path)
    settings.DEVICE_IDENTITY_PATH = str(identity_path)
    real_datetime = dt.datetime
    now_epoch = int(real_datetime.now(dt.timezone.utc).timestamp())
    timestamp = str(now_epoch + 30)
    nonce = "a" * 32
    from app.core.security import _canonical_cloud_request

    canonical = _canonical_cloud_request(
        device_id=identity["device_id"],
        method="GET",
        path="/admin",
        query=[],
        body=None,
        timestamp=timestamp,
        nonce=nonce,
    )
    headers = {
        "X-Drycontroller-Cloud-Device": identity["device_id"],
        "X-Drycontroller-Cloud-Timestamp": timestamp,
        "X-Drycontroller-Cloud-Nonce": nonce,
        "X-Drycontroller-Cloud-Signature": base64.b64encode(private_key.sign(canonical)).decode("ascii"),
    }
    client = _build_app()

    class FixedDateTime(real_datetime):
        current_epoch = now_epoch

        @classmethod
        def now(cls, tz=None):
            return real_datetime.fromtimestamp(cls.current_epoch, tz=tz)

    from app.core import security

    monkeypatch.setattr(security.dt, "datetime", FixedDateTime)

    assert client.get("/admin", headers=headers).status_code == 200
    security._CLOUD_NONCE_LOCK = asyncio.Lock()
    FixedDateTime.current_epoch = now_epoch + 31
    assert client.get("/admin", headers=headers).status_code == 409
    assert client.get("/control", headers={**headers, "X-Drycontroller-Cloud-Nonce": "b" * 32}).status_code == 401


def test_cloud_signature_concurrent_replay_is_rejected(tmp_path):
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public_path = tmp_path / "cloud.pub"
    public_path.write_text(
        base64.b64encode(private_key.public_key().public_bytes_raw()).decode("ascii"),
        encoding="ascii",
    )
    public_path.chmod(0o644)
    identity_path = tmp_path / "device.json"
    identity = {
        "schema": "drycontroller.device-identity.v1",
        "device_id": "EDGE-ABCDEF123456",
        "tenant_id": "tenant-a",
        "line_id": "LINE-A",
        "node_name": "edge-abcdef123456",
        "identity_fingerprint": "ab" * 32,
        "runtime_credential": "runtime-secret",
        "runtime_credential_hash": hashlib.sha256(b"runtime-secret").hexdigest(),
        "credential_expires_at": (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        ).isoformat(),
    }
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    identity_path.chmod(0o600)
    settings.CLOUD_IDENTITY_PUBLIC_KEY_FILE = str(public_path)
    settings.DEVICE_IDENTITY_PATH = str(identity_path)
    timestamp = str(int(dt.datetime.now(dt.timezone.utc).timestamp()))
    nonce = "c" * 32
    from app.core.security import _canonical_cloud_request

    canonical = _canonical_cloud_request(
        device_id=identity["device_id"],
        method="GET",
        path="/admin",
        query=[],
        body=None,
        timestamp=timestamp,
        nonce=nonce,
    )
    headers = {
        "X-Drycontroller-Cloud-Device": identity["device_id"],
        "X-Drycontroller-Cloud-Timestamp": timestamp,
        "X-Drycontroller-Cloud-Nonce": nonce,
        "X-Drycontroller-Cloud-Signature": base64.b64encode(private_key.sign(canonical)).decode("ascii"),
    }

    async def send_twice() -> list[int]:
        transport = httpx.ASGITransport(app=_create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(
                client.get("/admin", headers=headers),
                client.get("/admin", headers=headers),
            )
        return [response.status_code for response in responses]

    assert sorted(asyncio.run(send_twice())) == [200, 409]


def test_cloud_nonce_store_has_dedicated_writable_mount():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    installer = (ROOT / "infra/bootstrap/install-edge-host.sh").read_text(encoding="utf-8")

    assert "CLOUD_REQUEST_NONCE_DB=/var/lib/drycontroller-cloud-request-nonces/nonces.sqlite3" in compose
    assert (
        "/var/lib/drycontroller/cloud-request-nonces:/var/lib/drycontroller-cloud-request-nonces"
        in compose
    )
    assert "install -d -o root -g root -m 0700 /var/lib/drycontroller/cloud-request-nonces" in installer
