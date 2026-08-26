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
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]
for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

from app.core.config import settings  # noqa: E402
from app.core.security import (  # noqa: E402
    TAILNET_INGRESS_HEADER,
    require_admin_api_key,
    require_control_api_key,
    require_readonly_http_source,
    require_tailnet_http_auth,
)


def _create_app() -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def tailnet_auth(request: Request, call_next):
        try:
            await require_tailnet_http_auth(request)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers,
            )
        return await call_next(request)

    @app.get("/public")
    async def public():
        return {"ok": True}

    @app.get("/control")
    async def control(_: str = Depends(require_control_api_key)):
        return {"ok": True}

    @app.post("/control-body")
    async def control_body(payload: dict, _: str = Depends(require_control_api_key)):
        return payload

    @app.get("/admin")
    async def admin(_: str = Depends(require_admin_api_key)):
        return {"ok": True}

    @app.get("/readonly", dependencies=[Depends(require_readonly_http_source)])
    async def readonly():
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


def test_tailnet_marker_requires_application_auth_for_unprotected_route():
    settings.CONTROL_API_KEYS = ["ctl-1"]
    client = _build_app()

    unsigned = client.get("/public", headers={TAILNET_INGRESS_HEADER: "1"})
    authenticated = client.get(
        "/public",
        headers={TAILNET_INGRESS_HEADER: "1", settings.API_KEY_HEADER: "ctl-1"},
    )

    assert unsigned.status_code == 403
    assert authenticated.status_code == 200


def test_tailnet_control_key_does_not_gain_admin_role():
    settings.CONTROL_API_KEYS = ["ctl-1"]
    settings.ADMIN_API_KEYS = ["admin-1"]
    client = _build_app()

    response = client.get(
        "/admin",
        headers={TAILNET_INGRESS_HEADER: "1", settings.API_KEY_HEADER: "ctl-1"},
    )

    assert response.status_code == 403


def test_main_top_level_routes_enforce_tailnet_auth():
    from app.main import app as edge_app

    settings.CONTROL_API_KEYS = ["ctl-1"]
    client = TestClient(edge_app)
    for path in ("/", "/health", "/metrics", "/openapi.json"):
        unsigned = client.get(path, headers={TAILNET_INGRESS_HEADER: "1"})
        authenticated = client.get(
            path,
            headers={TAILNET_INGRESS_HEADER: "1", settings.API_KEY_HEADER: "ctl-1"},
        )
        assert unsigned.status_code == 403, path
        assert authenticated.status_code == 200, path


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
    readonly_nonce = "d" * 32
    readonly_canonical = _canonical_cloud_request(
        device_id=identity["device_id"],
        method="GET",
        path="/readonly",
        query=[],
        body=None,
        timestamp=timestamp,
        nonce=readonly_nonce,
    )
    readonly_headers = {
        **headers,
        TAILNET_INGRESS_HEADER: "1",
        "X-Drycontroller-Cloud-Nonce": readonly_nonce,
        "X-Drycontroller-Cloud-Signature": base64.b64encode(
            private_key.sign(readonly_canonical)
        ).decode("ascii"),
    }
    assert client.get("/readonly", headers=readonly_headers).status_code == 200
    body = {"line": "L1", "value": 31}
    body_nonce = "e" * 32
    body_canonical = _canonical_cloud_request(
        device_id=identity["device_id"],
        method="POST",
        path="/control-body",
        query=[],
        body=body,
        timestamp=timestamp,
        nonce=body_nonce,
    )
    body_headers = {
        **headers,
        TAILNET_INGRESS_HEADER: "1",
        "X-Drycontroller-Cloud-Nonce": body_nonce,
        "X-Drycontroller-Cloud-Signature": base64.b64encode(
            private_key.sign(body_canonical)
        ).decode("ascii"),
    }
    body_response = client.post("/control-body", headers=body_headers, json=body)
    assert body_response.status_code == 200
    assert body_response.json() == body
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
    path = ROOT / "infra/bootstrap/install-static-runtime.py"
    spec = importlib.util.spec_from_file_location("nonce_static_installer", path)
    assert spec is not None and spec.loader is not None
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)

    assert "CLOUD_REQUEST_NONCE_DB=/var/lib/drycontroller-cloud-request-nonces/nonces.sqlite3" in compose
    assert (
        "/var/lib/drycontroller/cloud-request-nonces:/var/lib/drycontroller-cloud-request-nonces"
        in compose
    )
    assert installer.DIRECTORIES["/var/lib/drycontroller/cloud-request-nonces"] == 0o700
