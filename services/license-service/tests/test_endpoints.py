"""
license-service API 测试
"""

import sys
from pathlib import Path

from fastapi.testclient import TestClient


PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]   # license-service/
ROOT = PATH.parents[3]           # Drycontroller-edge/ (项目根，含 common/)

for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

from license.main import app  # noqa: E402


client = TestClient(app)


PUBLIC_KEY = "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="
SIGNED_LICENSE = {
    "device_id": "EDGE-0001",
    "expires_at": "2099-01-01T00:00:00+00:00",
    "features": {"ai_softsensor": True, "control_optimizer": True, "edge_runtime": True, "ota": True},
    "issued_at": "2026-01-01T00:00:00+00:00",
    "issuer": "drycontroller-cloud-ops",
    "key_id": "test-key",
    "license_id": "lic_test",
    "line_id": "L1",
    "machine_id": "test-machine",
    "node_name": "edge-0001",
    "schema": "drycontroller.license.v1",
    "signature": "lyl3M6RV7ulriukI3n40uM1PUGQKdb69OBz+X33powrSY4P5YA6B8cYwwHoV5BKlfZ5HxzuHY0nFqZPWEqE9Dg==",
    "signature_alg": "ed25519",
    "tenant_id": "t-test",
    "ts_hostname": "edge-0001",
}


def test_health_endpoint():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_ready_endpoint():
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["ready"] is True


def test_verify_endpoint_valid(tmp_path, monkeypatch):
    import json

    license_path = tmp_path / "license.json"
    public_key_path = tmp_path / "license.pub"
    license_path.write_text(json.dumps(SIGNED_LICENSE), encoding="utf-8")
    public_key_path.write_text(PUBLIC_KEY, encoding="utf-8")
    monkeypatch.setenv("LICENSE_CHECK_ENABLED", "true")
    monkeypatch.setenv("LICENSE_FILE_PATH", str(license_path))
    monkeypatch.setenv("LICENSE_PUBLIC_KEY_PATH", str(public_key_path))
    monkeypatch.setenv("LICENSE_MACHINE_ID", "test-machine")

    resp = client.get("/verify")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["valid"] is True
    assert data["reason"] == "ok"


def test_verify_endpoint_missing_license(tmp_path, monkeypatch):
    public_key_path = tmp_path / "license.pub"
    public_key_path.write_text(PUBLIC_KEY, encoding="utf-8")
    monkeypatch.setenv("LICENSE_CHECK_ENABLED", "true")
    monkeypatch.setenv("LICENSE_FILE_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("LICENSE_PUBLIC_KEY_PATH", str(public_key_path))

    resp = client.get("/verify")
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert data["reason"] == "missing_license_file"


def test_verify_endpoint_machine_mismatch(tmp_path, monkeypatch):
    import json

    license_path = tmp_path / "license.json"
    public_key_path = tmp_path / "license.pub"
    license_path.write_text(json.dumps(SIGNED_LICENSE), encoding="utf-8")
    public_key_path.write_text(PUBLIC_KEY, encoding="utf-8")
    monkeypatch.setenv("LICENSE_CHECK_ENABLED", "true")
    monkeypatch.setenv("LICENSE_FILE_PATH", str(license_path))
    monkeypatch.setenv("LICENSE_PUBLIC_KEY_PATH", str(public_key_path))
    monkeypatch.setenv("LICENSE_MACHINE_ID", "other-machine")

    resp = client.get("/verify")
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert data["reason"] == "machine_mismatch"


def test_verify_endpoint_disabled(monkeypatch):
    monkeypatch.setenv("LICENSE_CHECK_ENABLED", "false")

    resp = client.get("/verify")
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert data["reason"] == "license_disabled"
