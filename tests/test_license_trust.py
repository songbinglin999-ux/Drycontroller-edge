from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "scripts" / "manage-license-trust.py"


def _trust(private_key: Ed25519PrivateKey, key_id: str) -> bytes:
    der = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    payload = {
        "algorithm": "Ed25519",
        "key_id": key_id,
        "public_key_sha256": "sha256:" + hashlib.sha256(der).hexdigest(),
        "public_key_spki_der_b64": base64.b64encode(der).decode("ascii"),
        "schema": "drycontroller.license-trust.v1",
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _signed_license(private_key: Ed25519PrivateKey, key_id: str) -> dict[str, str]:
    from common.license_verifier import canonical_license_bytes

    payload = {
        "expires_at": "2099-01-01T00:00:00Z",
        "issued_at": "2026-01-01T00:00:00Z",
        "key_id": key_id,
        "license_id": "lic-1",
        "machine_id": "machine-1",
        "signature_alg": "ed25519",
    }
    return {
        **payload,
        "signature": base64.b64encode(
            private_key.sign(canonical_license_bytes(payload))
        ).decode("ascii"),
    }


def test_canonical_license_trust_parses_spki_and_rejects_noncanonical_bytes():
    from common.license_verifier import parse_license_trust

    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    canonical = _trust(private_key, "license-old")
    trust = parse_license_trust(canonical)

    assert trust.key_id == "license-old"
    assert len(trust.public_key_raw) == 32
    with pytest.raises(ValueError, match="canonical"):
        parse_license_trust(canonical + b"\n")


def test_license_trust_rejects_digest_mismatch_and_unknown_field():
    from common.license_verifier import parse_license_trust

    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    payload = json.loads(_trust(private_key, "license-old"))
    payload["public_key_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="digest"):
        parse_license_trust(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )

    payload = json.loads(_trust(private_key, "license-old"))
    payload["legacy_raw_public_key"] = "forbidden"
    with pytest.raises(ValueError, match="fields"):
        parse_license_trust(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )


def test_atomic_policy_rotates_and_rolls_back_without_dual_trust(tmp_path: Path):
    from common.license_verifier import load_active_license_trust, verify_license

    old_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    new_key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    old_artifact = tmp_path / "old.json"
    new_artifact = tmp_path / "new.json"
    old_artifact.write_bytes(_trust(old_key, "license-old"))
    new_artifact.write_bytes(_trust(new_key, "license-new"))
    root = tmp_path / "license-trust"

    subprocess.run(
        [sys.executable, str(MANAGER), "--root", str(root), "initialize", str(old_artifact)],
        check=True,
    )
    old_license = _signed_license(old_key, "license-old")
    new_license = _signed_license(new_key, "license-new")
    verify_license(old_license, load_active_license_trust(root / "policy.json"))
    with pytest.raises(ValueError, match="key_id"):
        verify_license(new_license, load_active_license_trust(root / "policy.json"))

    subprocess.run(
        [
            sys.executable,
            str(MANAGER),
            "--root",
            str(root),
            "rotate",
            str(new_artifact),
            "--expected-active",
            "license-old",
        ],
        check=True,
    )
    verify_license(new_license, load_active_license_trust(root / "policy.json"))
    with pytest.raises(ValueError, match="key_id"):
        verify_license(old_license, load_active_license_trust(root / "policy.json"))

    subprocess.run(
        [
            sys.executable,
            str(MANAGER),
            "--root",
            str(root),
            "rollback",
            "--expected-active",
            "license-new",
        ],
        check=True,
    )
    verify_license(old_license, load_active_license_trust(root / "policy.json"))
    with pytest.raises(ValueError, match="key_id"):
        verify_license(new_license, load_active_license_trust(root / "policy.json"))

    policy = json.loads((root / "policy.json").read_text(encoding="utf-8"))
    assert policy["active"]["key_id"] == "license-old"
    assert policy["previous"]["key_id"] == "license-new"


def test_rotation_rejects_reusing_key_id_with_different_public_key(tmp_path: Path):
    old_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    replacement_key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    old_artifact = tmp_path / "old.json"
    replacement_artifact = tmp_path / "replacement.json"
    old_artifact.write_bytes(_trust(old_key, "license-prod"))
    replacement_artifact.write_bytes(_trust(replacement_key, "license-prod"))
    root = tmp_path / "license-trust"

    subprocess.run(
        [sys.executable, str(MANAGER), "--root", str(root), "initialize", str(old_artifact)],
        check=True,
    )
    result = subprocess.run(
        [
            sys.executable,
            str(MANAGER),
            "--root",
            str(root),
            "rotate",
            str(replacement_artifact),
            "--expected-active",
            "license-prod",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "already exists with different bytes" in result.stderr
