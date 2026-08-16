from __future__ import annotations

import base64
import fcntl
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


def test_initializer_creates_policy_once_and_exposes_no_mutation_commands(tmp_path: Path):
    old_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    new_key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    old_artifact = tmp_path / "old.json"
    new_artifact = tmp_path / "new.json"
    old_artifact.write_bytes(_trust(old_key, "license-old"))
    new_artifact.write_bytes(_trust(new_key, "license-new"))
    root = tmp_path / "license-trust"

    subprocess.run(
        [sys.executable, str(MANAGER), "--root", str(root), str(old_artifact)],
        check=True,
    )
    before = (root / "policy.json").read_bytes()
    second = subprocess.run(
        [
            sys.executable,
            str(MANAGER),
            "--root",
            str(root),
            str(new_artifact),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    help_result = subprocess.run(
        [
            sys.executable,
            str(MANAGER),
            "--help",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    release_lock = tmp_path / "release.lock"
    with release_lock.open("wb") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        retired_rotate = subprocess.run(
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
            check=False,
            capture_output=True,
            text=True,
        )

    assert second.returncode != 0
    assert "already initialized" in second.stderr
    assert (root / "policy.json").read_bytes() == before
    for retired in ("rotate", "rollback", "revoke-previous", "status"):
        assert retired not in help_result.stdout
    assert retired_rotate.returncode != 0
    assert (root / "policy.json").read_bytes() == before
