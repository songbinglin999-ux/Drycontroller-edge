"""
edge-api License 安装闭环测试。

目标：云端下发 License 后，边端可无人值守安装；新 License 校验失败时必须恢复旧文件。
"""

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]
for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

from app.api.v1 import ops as ops_module  # noqa: E402
from app.core.config import settings  # noqa: E402
from common.license_verifier import canonical_license_bytes  # noqa: E402


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(ops_module.router, prefix="/api/v1/ops")
    return TestClient(app)


def _signed_payload() -> dict:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    license_payload = {
        "license_id": "lic-1",
        "machine_id": "machine-1",
        "issued_at": "2026-01-01T00:00:00Z",
        "expires_at": "2027-01-01T00:00:00Z",
        "signature_alg": "ed25519",
    }
    signature = private_key.sign(canonical_license_bytes(license_payload))
    import base64

    return {
        "license": {**license_payload, "signature": base64.b64encode(signature).decode("ascii")},
        "license_public_key": base64.b64encode(public_key).decode("ascii"),
    }


def test_license_install_success_writes_files(tmp_path: Path, monkeypatch):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    settings.LICENSE_FILE_PATH = str(tmp_path / "license.json")
    settings.LICENSE_PUBLIC_KEY_PATH = str(tmp_path / "license.pub")
    settings.LICENSE_TRUSTED_PUBLIC_KEY = _signed_payload()["license_public_key"]

    async def fake_verify():
        return {"url": "mock", "status_code": 200, "body": {"valid": True}}

    monkeypatch.setattr(ops_module, "_call_license_verify", fake_verify)

    resp = _client().post(
        "/api/v1/ops/license/install",
        json=_signed_payload(),
        headers={"X-API-Key": "admin-1"},
    )

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert "lic-1" in Path(settings.LICENSE_FILE_PATH).read_text(encoding="utf-8")
    assert Path(settings.LICENSE_PUBLIC_KEY_PATH).read_text(encoding="utf-8").strip() == _signed_payload()["license_public_key"]


def test_license_install_rolls_back_when_verify_fails(tmp_path: Path, monkeypatch):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    license_path = tmp_path / "license.json"
    public_key_path = tmp_path / "license.pub"
    settings.LICENSE_FILE_PATH = str(license_path)
    settings.LICENSE_PUBLIC_KEY_PATH = str(public_key_path)
    settings.LICENSE_TRUSTED_PUBLIC_KEY = _signed_payload()["license_public_key"]
    license_path.write_text('{"old": true}\n', encoding="utf-8")
    public_key_path.write_text("old-key\n", encoding="utf-8")

    calls = 0

    async def fake_verify():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"url": "mock", "status_code": 200, "body": {"valid": False, "reason": "bad_signature"}}
        return {"url": "mock", "status_code": 200, "body": {"valid": True}}

    monkeypatch.setattr(ops_module, "_call_license_verify", fake_verify)

    resp = _client().post(
        "/api/v1/ops/license/install",
        json=_signed_payload(),
        headers={"X-API-Key": "admin-1"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "license_verify_failed_after_install"
    assert license_path.read_text(encoding="utf-8") == '{"old": true}\n'
    assert public_key_path.read_text(encoding="utf-8") == "old-key\n"


def test_license_install_rejects_malformed_public_key(tmp_path: Path):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    settings.LICENSE_FILE_PATH = str(tmp_path / "license.json")
    settings.LICENSE_PUBLIC_KEY_PATH = str(tmp_path / "license.pub")
    settings.LICENSE_TRUSTED_PUBLIC_KEY = _signed_payload()["license_public_key"]
    payload = _signed_payload()
    payload["license_public_key"] = "not base64!"

    resp = _client().post(
        "/api/v1/ops/license/install",
        json=payload,
        headers={"X-API-Key": "admin-1"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_license_public_key_base64"


def test_license_install_rejects_untrusted_public_key(tmp_path: Path):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    settings.LICENSE_FILE_PATH = str(tmp_path / "license.json")
    settings.LICENSE_PUBLIC_KEY_PATH = str(tmp_path / "license.pub")
    settings.LICENSE_TRUSTED_PUBLIC_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

    resp = _client().post(
        "/api/v1/ops/license/install",
        json=_signed_payload(),
        headers={"X-API-Key": "admin-1"},
    )

    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "license_public_key_not_trusted"


def test_license_install_rejects_bad_signature(tmp_path: Path):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    settings.LICENSE_FILE_PATH = str(tmp_path / "license.json")
    settings.LICENSE_PUBLIC_KEY_PATH = str(tmp_path / "license.pub")
    payload = _signed_payload()
    settings.LICENSE_TRUSTED_PUBLIC_KEY = payload["license_public_key"]
    payload["license"]["machine_id"] = "tampered"

    resp = _client().post(
        "/api/v1/ops/license/install",
        json=payload,
        headers={"X-API-Key": "admin-1"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "license_signature_invalid"


def test_preflight_returns_pass_with_degraded_allowed(tmp_path: Path, monkeypatch):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    settings.LICENSE_FILE_PATH = str(tmp_path / "license.json")
    settings.LICENSE_PUBLIC_KEY_PATH = str(tmp_path / "license.pub")
    Path(settings.LICENSE_FILE_PATH).write_text("{}", encoding="utf-8")
    Path(settings.LICENSE_PUBLIC_KEY_PATH).write_text("pub", encoding="utf-8")
    device_json = tmp_path / "device.json"
    device_json.write_text('{"device_id":"d1","tenant_id":"t1","node_name":"n1"}', encoding="utf-8")
    sentinel = tmp_path / ".bootstrapped"
    sentinel.write_text("ok", encoding="utf-8")
    monkeypatch.setenv("PREFLIGHT_DEVICE_JSON", str(device_json))
    monkeypatch.setenv("PREFLIGHT_BOOTSTRAP_SENTINEL", str(sentinel))
    monkeypatch.setattr(settings, "CLOUD_OPS_BASE_URL", "http://cloud")
    monkeypatch.setenv("CLOUD_OPS_ENROLL_TOKEN", "token")
    monkeypatch.setattr(settings, "TS_LOGIN_SERVER", "http://headscale")

    async def fake_verify():
        return {"url": "mock", "status_code": 200, "body": {"valid": True}}

    async def fake_http_check(checks, check_id, url, *, degraded_ok, allow_degraded, timeout=5.0):
        ops_module._preflight_check(checks, check_id, "pass", "ok", detail={"url": url})

    monkeypatch.setattr(ops_module, "_call_license_verify", fake_verify)
    monkeypatch.setattr(ops_module, "_preflight_http_check", fake_http_check)
    recorded = []

    async def fake_record_preflight_run(payload):
        recorded.append(payload)

    monkeypatch.setattr(ops_module, "record_preflight_run", fake_record_preflight_run)

    resp = _client().post(
        "/api/v1/ops/preflight",
        json={"strict": False, "check_ota": False},
        headers={"X-API-Key": "admin-1"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["status"] == "degraded"
    assert body["summary"]["fail"] == 0
    assert recorded[0]["status"] == "degraded"


def test_preflight_fails_when_required_files_missing(tmp_path: Path, monkeypatch):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    settings.LICENSE_FILE_PATH = str(tmp_path / "missing-license.json")
    settings.LICENSE_PUBLIC_KEY_PATH = str(tmp_path / "missing-license.pub")
    monkeypatch.setenv("PREFLIGHT_DEVICE_JSON", str(tmp_path / "missing-device.json"))
    monkeypatch.setenv("PREFLIGHT_BOOTSTRAP_SENTINEL", str(tmp_path / "missing-sentinel"))

    async def fake_verify():
        return {"url": "mock", "status_code": 200, "body": {"valid": False}}

    async def fake_http_check(checks, check_id, url, *, degraded_ok, allow_degraded, timeout=5.0):
        ops_module._preflight_check(checks, check_id, "pass", "ok", detail={"url": url})

    monkeypatch.setattr(ops_module, "_call_license_verify", fake_verify)
    monkeypatch.setattr(ops_module, "_preflight_http_check", fake_http_check)
    recorded = []

    async def fake_record_preflight_run(payload):
        recorded.append(payload)

    monkeypatch.setattr(ops_module, "record_preflight_run", fake_record_preflight_run)

    resp = _client().post(
        "/api/v1/ops/preflight",
        json={"strict": False, "check_ota": False},
        headers={"X-API-Key": "admin-1"},
    )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["success"] is False
    assert detail["status"] == "fail"
    assert detail["summary"]["fail"] >= 1
    assert recorded[0]["status"] == "fail"


def test_plc_config_apply_records_version(monkeypatch):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    written = []
    recorded = []

    def fake_atomic_write_yaml(path, payload):
        written.append((path, payload))

    async def fake_record_config_version(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(ops_module, "_atomic_write_yaml", fake_atomic_write_yaml)
    monkeypatch.setattr(ops_module, "record_config_version", fake_record_config_version)

    resp = _client().post(
        "/api/v1/ops/plc-config/apply",
        json={
            "plc_host": "192.168.1.20",
            "restart_edge_daq": False,
            "read_ready_after_restart": False,
        },
        headers={"X-API-Key": "admin-1"},
    )

    assert resp.status_code == 200
    assert written[0][1]["host"] == "192.168.1.20"
    assert recorded[0]["scope"] == "plc"
    assert recorded[0]["source"] == "cloud"
    assert recorded[0]["restart_required"] is True


def test_plc_config_apply_records_pending_restart_when_restart_fails(monkeypatch):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    recorded = []

    class _FailingMonitorClient:
        async def restart_service(self, *_args, **_kwargs):
            raise RuntimeError("restart failed")

    async def fake_record_config_version(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(ops_module, "_atomic_write_yaml", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ops_module, "record_config_version", fake_record_config_version)
    app = FastAPI()
    app.include_router(ops_module.router, prefix="/api/v1/ops")
    app.dependency_overrides[ops_module.get_monitor_ops_client] = lambda: _FailingMonitorClient()

    with pytest.raises(RuntimeError, match="restart failed"):
        TestClient(app).post(
            "/api/v1/ops/plc-config/apply",
            json={"plc_host": "192.168.1.20"},
            headers={"X-API-Key": "admin-1"},
        )

    assert recorded[0]["scope"] == "plc"
    assert recorded[0]["restart_required"] is True
