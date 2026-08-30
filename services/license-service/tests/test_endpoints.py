"""
license-service API 测试
"""

import sys
import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi.testclient import TestClient


PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]   # license-service/
ROOT = PATH.parents[3]           # Drycontroller-edge/ (项目根，含 common/)

for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

from license import main as license_main  # noqa: E402


app = license_main.app
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


def _write_trust_policy(root: Path) -> Path:
    public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY))
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    artifact = {
        "algorithm": "Ed25519",
        "key_id": "test-key",
        "public_key_sha256": "sha256:" + hashlib.sha256(der).hexdigest(),
        "public_key_spki_der_b64": base64.b64encode(der).decode("ascii"),
        "schema": "drycontroller.license-trust.v1",
    }
    artifact_bytes = json.dumps(
        artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    version = root / "versions" / "test-key"
    version.mkdir(parents=True)
    (version / "license-trust.json").write_bytes(artifact_bytes)
    policy = {
        "active": {
            "canonical_sha256": "sha256:" + hashlib.sha256(artifact_bytes).hexdigest(),
            "key_id": "test-key",
        },
        "previous": None,
        "schema": "drycontroller.license-trust-policy.v1",
    }
    policy_path = root / "policy.json"
    policy_path.write_text(
        json.dumps(policy, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    return policy_path


def test_health_endpoint():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_ready_endpoint():
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["ready"] is True


def test_verify_endpoint_rejects_legacy_v1_license(tmp_path, monkeypatch):
    license_path = tmp_path / "license.json"
    trust_policy_path = _write_trust_policy(tmp_path / "license-trust")
    license_path.write_text(json.dumps(SIGNED_LICENSE), encoding="utf-8")
    monkeypatch.setenv("LICENSE_CHECK_ENABLED", "true")
    monkeypatch.setenv("LICENSE_FILE_PATH", str(license_path))
    monkeypatch.setenv("LICENSE_TRUST_POLICY_PATH", str(trust_policy_path))

    resp = client.get("/verify")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["valid"] is False
    assert data["reason"] == "signature_invalid"


def test_verify_endpoint_missing_license(tmp_path, monkeypatch):
    trust_policy_path = _write_trust_policy(tmp_path / "license-trust")
    monkeypatch.setenv("LICENSE_CHECK_ENABLED", "true")
    monkeypatch.setenv("LICENSE_FILE_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("LICENSE_TRUST_POLICY_PATH", str(trust_policy_path))

    resp = client.get("/verify")
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert data["reason"] == "missing_license_file"


def test_verify_endpoint_does_not_restore_machine_id_compatibility(tmp_path, monkeypatch):
    license_path = tmp_path / "license.json"
    trust_policy_path = _write_trust_policy(tmp_path / "license-trust")
    license_path.write_text(json.dumps(SIGNED_LICENSE), encoding="utf-8")
    monkeypatch.setenv("LICENSE_CHECK_ENABLED", "true")
    monkeypatch.setenv("LICENSE_FILE_PATH", str(license_path))
    monkeypatch.setenv("LICENSE_TRUST_POLICY_PATH", str(trust_policy_path))
    monkeypatch.setenv("LICENSE_MACHINE_ID", "other-machine")

    resp = client.get("/verify")
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert data["reason"] == "signature_invalid"


def test_retained_service_rejects_v2_without_live_tpm_surface(tmp_path, monkeypatch):
    license_path = tmp_path / "license.json"
    trust_policy_path = _write_trust_policy(tmp_path / "license-trust")
    payload = {
        "capabilities": ["cloud-session", "edge-app-ota", "production-control"],
        "device_id": "EDGE-0001",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "identity_fingerprint": "1" * 64,
        "issued_at": "2026-01-01T00:00:00+00:00",
        "issuer": "drycontroller-cloud-ops",
        "key_id": "test-key",
        "license_id": "lic_" + "1" * 32,
        "line_id": "L1",
        "node_name": "edge-0001",
        "previous_license_id": None,
        "product": "edge-app-v1",
        "schema": "drycontroller.license.v2",
        "signature": "test-only",
        "signature_alg": "ed25519",
        "tenant_id": "t-test",
    }
    license_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("LICENSE_CHECK_ENABLED", "true")
    monkeypatch.setenv("LICENSE_FILE_PATH", str(license_path))
    monkeypatch.setenv("LICENSE_TRUST_POLICY_PATH", str(trust_policy_path))
    monkeypatch.setattr(license_main, "verify_signed_license", lambda *_args: None)

    resp = client.get("/verify")

    assert resp.status_code == 200
    assert resp.json()["valid"] is False
    assert resp.json()["reason"] == "tpm_identity_mismatch"


def test_verify_endpoint_disabled(monkeypatch):
    monkeypatch.setenv("LICENSE_CHECK_ENABLED", "false")

    resp = client.get("/verify")
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert data["reason"] == "license_disabled"
