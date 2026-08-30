from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from common.license_verifier import canonical_license_bytes, verify_installed_license


def _write_fixture(root: Path) -> dict[str, Path]:
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    spki = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    trust = {
        "algorithm": "Ed25519",
        "key_id": "license-test",
        "public_key_sha256": "sha256:" + hashlib.sha256(spki).hexdigest(),
        "public_key_spki_der_b64": base64.b64encode(spki).decode(),
        "schema": "drycontroller.license-trust.v1",
    }
    trust_raw = json.dumps(trust, sort_keys=True, separators=(",", ":")).encode()
    trust_dir = root / "trust" / "versions" / "license-test"
    trust_dir.mkdir(parents=True)
    (trust_dir / "license-trust.json").write_bytes(trust_raw)
    policy = {
        "active": {
            "canonical_sha256": "sha256:" + hashlib.sha256(trust_raw).hexdigest(),
            "key_id": "license-test",
        },
        "previous": None,
        "schema": "drycontroller.license-trust-policy.v1",
    }
    policy_path = root / "trust" / "policy.json"
    policy_path.write_text(json.dumps(policy, sort_keys=True, separators=(",", ":")))
    identity = {
        "device_id": "EDGE-ABCDEF123456",
        "tenant_id": "tenant-a",
        "line_id": "LINE-A",
        "node_name": "edge-abcdef123456",
        "identity_fingerprint": "11" * 32,
    }
    identity_path = root / "device.json"
    identity_path.write_text(json.dumps(identity))
    observation = {
        "schema": "drycontroller.tpm-identity-observation.v1",
        "status": "ok",
        "identity_fingerprint": identity["identity_fingerprint"],
        "observed_at": "2026-08-30T00:00:00Z",
    }
    observation_path = root / "observation.json"
    observation_path.write_text(json.dumps(observation))
    license_payload = {
        "capabilities": ["cloud-session", "edge-app-ota", "production-control"],
        "device_id": identity["device_id"],
        "expires_at": "2026-09-30T00:00:00Z",
        "identity_fingerprint": identity["identity_fingerprint"],
        "issued_at": "2026-08-30T00:00:00Z",
        "issuer": "drycontroller-cloud-ops",
        "key_id": "license-test",
        "license_id": "lic_" + "a" * 32,
        "line_id": identity["line_id"],
        "node_name": identity["node_name"],
        "previous_license_id": None,
        "product": "edge-app-v1",
        "schema": "drycontroller.license.v2",
        "signature_alg": "ed25519",
        "tenant_id": identity["tenant_id"],
    }
    license_payload["signature"] = base64.b64encode(
        key.sign(canonical_license_bytes(license_payload))
    ).decode()
    license_path = root / "license.json"
    license_path.write_text(json.dumps(license_payload))
    return {
        "license_path": license_path,
        "trust_policy_path": policy_path,
        "device_identity_path": identity_path,
        "tpm_observation_path": observation_path,
    }


def _verify(paths: dict[str, Path]) -> dict:
    return verify_installed_license(
        **paths,
        required_capability="edge-app-ota",
        now=dt.datetime(2026, 8, 30, 1, tzinfo=dt.UTC),
    )


def test_v2_license_requires_signature_device_and_live_tpm(tmp_path: Path):
    paths = _write_fixture(tmp_path)
    assert _verify(paths)["valid"] is True

    observation = json.loads(paths["tpm_observation_path"].read_text())
    observation["identity_fingerprint"] = "22" * 32
    paths["tpm_observation_path"].write_text(json.dumps(observation))
    rejected = _verify(paths)
    assert rejected == {"valid": False, "reason": "tpm_identity_mismatch"}


def test_v2_license_rejects_tampering_and_legacy_shape(tmp_path: Path):
    paths = _write_fixture(tmp_path)
    payload = json.loads(paths["license_path"].read_text())
    payload["device_id"] = "EDGE-000000000000"
    paths["license_path"].write_text(json.dumps(payload))
    assert _verify(paths)["reason"] == "signature_invalid"

    paths["license_path"].write_text(json.dumps({"machine_id": "legacy"}))
    assert _verify(paths)["reason"] == "license fields do not match the v2 schema"
