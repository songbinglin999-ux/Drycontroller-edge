from __future__ import annotations

import base64
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from common.license_verifier import (
    canonical_license_bytes,
    load_active_license_trust,
    verify_license,
)


ROOT = Path(__file__).resolve().parents[1]
TRUST_MANAGER = ROOT / "scripts" / "manage-license-trust.py"
TRANSACTION_PATH = ROOT / "scripts" / "manage-license-transaction.py"
SPEC = importlib.util.spec_from_file_location("manage_license_transaction", TRANSACTION_PATH)
assert SPEC is not None and SPEC.loader is not None
TRANSACTION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TRANSACTION)


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
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _license(private_key: Ed25519PrivateKey, key_id: str) -> bytes:
    unsigned = {
        "expires_at": "2099-01-01T00:00:00Z",
        "issued_at": "2026-01-01T00:00:00Z",
        "key_id": key_id,
        "license_id": f"lic-{key_id}",
        "machine_id": "machine-1",
        "signature_alg": "ed25519",
    }
    payload = {
        **unsigned,
        "signature": base64.b64encode(
            private_key.sign(canonical_license_bytes(unsigned))
        ).decode("ascii"),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _fixture(tmp_path: Path):
    old_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    new_key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    trust_root = tmp_path / "trust"
    old_trust = tmp_path / "old-trust.json"
    new_trust = tmp_path / "new-trust.json"
    candidate_license = tmp_path / "new-license.json"
    old_trust.write_bytes(_trust(old_key, "license-old"))
    new_trust.write_bytes(_trust(new_key, "license-new"))
    candidate_license.write_bytes(_license(new_key, "license-new"))
    subprocess.run(
        [
            sys.executable,
            str(TRUST_MANAGER),
            "--root",
            str(trust_root),
            "initialize",
            str(old_trust),
        ],
        check=True,
    )
    live_license = tmp_path / "live" / "license.json"
    live_license.parent.mkdir()
    live_license.write_bytes(_license(old_key, "license-old"))
    live_license.chmod(0o640)
    kwargs = {
        "trust_root": trust_root,
        "license_path": live_license,
        "state_root": tmp_path / "state",
        "lock_path": tmp_path / "release" / "release.lock",
        "expected_uid": os.getuid(),
    }
    return kwargs, old_key, new_key, new_trust, candidate_license


def _assert_live(kwargs, accepted: bytes, rejected: bytes) -> None:
    trust = load_active_license_trust(kwargs["trust_root"] / "policy.json")
    verify_license(json.loads(accepted), trust)
    with pytest.raises(ValueError, match="key_id"):
        verify_license(json.loads(rejected), trust)
    assert kwargs["license_path"].read_bytes() == accepted


def test_activation_and_rollback_switch_the_verified_pair(tmp_path: Path):
    kwargs, old_key, new_key, new_trust, candidate_license = _fixture(tmp_path)
    old_license = _license(old_key, "license-old")
    new_license = _license(new_key, "license-new")
    transaction = TRANSACTION.LicenseTransaction(**kwargs)

    transaction.activate(new_trust, candidate_license, "license-old")
    _assert_live(kwargs, new_license, old_license)
    state = json.loads((kwargs["state_root"] / "state.json").read_text())
    assert state["active"]["key_id"] == "license-new"
    assert state["previous"]["key_id"] == "license-old"

    transaction.rollback("license-new")
    _assert_live(kwargs, old_license, new_license)
    state = json.loads((kwargs["state_root"] / "state.json").read_text())
    assert state["active"]["key_id"] == "license-old"
    assert state["previous"]["key_id"] == "license-new"


@pytest.mark.parametrize(
    "boundary",
    ["after_prepared", "after_policy", "after_license", "after_verified", "after_state"],
)
def test_recovery_restores_old_pair_after_every_crash_boundary(
    tmp_path: Path, boundary: str
):
    kwargs, old_key, new_key, new_trust, candidate_license = _fixture(tmp_path)
    old_license = _license(old_key, "license-old")
    new_license = _license(new_key, "license-new")

    def crash(point: str) -> None:
        if point == boundary:
            raise TRANSACTION.InjectedCrash(point)

    with pytest.raises(TRANSACTION.InjectedCrash):
        TRANSACTION.LicenseTransaction(**kwargs, fault=crash).activate(
            new_trust, candidate_license, "license-old"
        )

    assert (kwargs["state_root"] / "journal.json").is_file()
    assert TRANSACTION.LicenseTransaction(**kwargs).recover() is True
    _assert_live(kwargs, old_license, new_license)
    assert not (kwargs["state_root"] / "journal.json").exists()
    assert TRANSACTION.LicenseTransaction(**kwargs).recover() is False


@pytest.mark.parametrize(
    "boundary",
    ["after_prepared", "after_policy", "after_license", "after_verified", "after_state"],
)
def test_rollback_recovery_restores_new_pair_after_every_crash_boundary(
    tmp_path: Path, boundary: str
):
    kwargs, old_key, new_key, new_trust, candidate_license = _fixture(tmp_path)
    old_license = _license(old_key, "license-old")
    new_license = _license(new_key, "license-new")
    TRANSACTION.LicenseTransaction(**kwargs).activate(
        new_trust, candidate_license, "license-old"
    )

    def crash(point: str) -> None:
        if point == boundary:
            raise TRANSACTION.InjectedCrash(point)

    with pytest.raises(TRANSACTION.InjectedCrash):
        TRANSACTION.LicenseTransaction(**kwargs, fault=crash).rollback("license-new")

    assert TRANSACTION.LicenseTransaction(**kwargs).recover() is True
    _assert_live(kwargs, new_license, old_license)
    assert not (kwargs["state_root"] / "journal.json").exists()


def test_candidate_pair_and_expected_active_are_fail_closed(tmp_path: Path):
    kwargs, old_key, _new_key, new_trust, candidate_license = _fixture(tmp_path)
    old_license = _license(old_key, "license-old")
    transaction = TRANSACTION.LicenseTransaction(**kwargs)

    with pytest.raises(ValueError, match="expected license-missing"):
        transaction.activate(new_trust, candidate_license, "license-missing")
    bad_license = tmp_path / "bad-license.json"
    bad_license.write_bytes(old_license)
    with pytest.raises(ValueError, match="key_id"):
        transaction.activate(new_trust, bad_license, "license-old")

    _assert_live(kwargs, old_license, candidate_license.read_bytes())
    assert not (kwargs["state_root"] / "journal.json").exists()


def test_shared_release_lock_rejects_concurrent_mutation(tmp_path: Path):
    kwargs, _old_key, _new_key, new_trust, candidate_license = _fixture(tmp_path)
    kwargs["lock_path"].parent.mkdir(mode=0o700)
    descriptor = os.open(kwargs["lock_path"], os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="another release transaction"):
            TRANSACTION.LicenseTransaction(**kwargs).activate(
                new_trust, candidate_license, "license-old"
            )
    finally:
        os.close(descriptor)


def test_production_cli_is_root_only_and_has_fixed_paths():
    source = TRANSACTION_PATH.read_text(encoding="utf-8")
    assert "os.geteuid() != 0" in source
    assert 'Path("/etc/drycontroller/keys/license-trust")' in source
    assert 'Path("/etc/drycontroller/license/license.json")' in source
    assert 'Path("/var/lib/drycontroller-ota/release.lock")' in source
    assert "--root" not in source


def test_boot_recovery_runs_before_docker_and_is_installed():
    unit = (
        ROOT / "infra/bootstrap/drycontroller-license-transaction-recovery.service"
    ).read_text(encoding="utf-8")
    installer = (ROOT / "infra/bootstrap/install-edge-host.sh").read_text(
        encoding="utf-8"
    )
    assert "Before=docker.service" in unit
    assert "manage-license-transaction.py recover" in unit
    assert "install -o root -g root -m 0755" in installer
    assert "systemctl enable drycontroller-license-transaction-recovery.service" in installer
