"""
license-service API 测试
"""

import sys
import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient


PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]   # license-service/
ROOT = PATH.parents[3]           # Drycontroller-edge/ (项目根，含 common/)

for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

from license.main import app  # noqa: E402
from common.license_verifier import canonical_license_bytes  # noqa: E402


client = TestClient(app)


PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
LICENSE = {
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
    "signature_alg": "ed25519",
    "tenant_id": "t-test",
}
LICENSE["signature"] = base64.b64encode(
    PRIVATE_KEY.sign(canonical_license_bytes(LICENSE))
).decode()


def _write_trust_policy(root: Path) -> Path:
    public_key = PRIVATE_KEY.public_key()
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


def _write_identity_observation(root: Path, *, device_id: str = "EDGE-0001") -> tuple[Path, Path]:
    identity = root / "device.json"
    observation = root / "tpm-observation.json"
    identity.write_text(
        json.dumps(
            {
                "device_id": device_id,
                "tenant_id": LICENSE["tenant_id"],
                "line_id": LICENSE["line_id"],
                "node_name": LICENSE["node_name"],
                "identity_fingerprint": LICENSE["identity_fingerprint"],
            }
        ),
        encoding="utf-8",
    )
    observation.write_text(
        json.dumps(
            {
                "schema": "drycontroller.tpm-identity-observation.v1",
                "status": "ok",
                "identity_fingerprint": LICENSE["identity_fingerprint"],
                "observed_at": "2026-08-30T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return identity, observation


def test_health_endpoint():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_ready_endpoint():
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["ready"] is True


def test_verify_endpoint_valid(tmp_path, monkeypatch):
    license_path = tmp_path / "license.json"
    trust_policy_path = _write_trust_policy(tmp_path / "license-trust")
    identity_path, observation_path = _write_identity_observation(tmp_path)
    license_path.write_text(json.dumps(LICENSE), encoding="utf-8")
    monkeypatch.setenv("LICENSE_CHECK_ENABLED", "true")
    monkeypatch.setenv("LICENSE_FILE_PATH", str(license_path))
    monkeypatch.setenv("LICENSE_TRUST_POLICY_PATH", str(trust_policy_path))
    monkeypatch.setenv("DEVICE_IDENTITY_PATH", str(identity_path))
    monkeypatch.setenv("TPM_IDENTITY_OBSERVATION_PATH", str(observation_path))

    resp = client.get("/verify")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["valid"] is True
    assert data["reason"] == "ok"


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


def test_verify_endpoint_device_identity_mismatch(tmp_path, monkeypatch):
    license_path = tmp_path / "license.json"
    trust_policy_path = _write_trust_policy(tmp_path / "license-trust")
    identity_path, observation_path = _write_identity_observation(
        tmp_path, device_id="EDGE-OTHER"
    )
    license_path.write_text(json.dumps(LICENSE), encoding="utf-8")
    monkeypatch.setenv("LICENSE_CHECK_ENABLED", "true")
    monkeypatch.setenv("LICENSE_FILE_PATH", str(license_path))
    monkeypatch.setenv("LICENSE_TRUST_POLICY_PATH", str(trust_policy_path))
    monkeypatch.setenv("DEVICE_IDENTITY_PATH", str(identity_path))
    monkeypatch.setenv("TPM_IDENTITY_OBSERVATION_PATH", str(observation_path))

    resp = client.get("/verify")
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert data["reason"] == "device_identity_mismatch"


def test_verify_endpoint_disabled(monkeypatch):
    monkeypatch.setenv("LICENSE_CHECK_ENABLED", "false")

    resp = client.get("/verify")
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert data["reason"] == "license_disabled"
