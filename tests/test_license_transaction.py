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


def _license(
    private_key: Ed25519PrivateKey,
    key_id: str,
    *,
    machine_id: str = "machine-1",
    issued_at: str = "2026-01-01T00:00:00Z",
    expires_at: str = "2099-01-01T00:00:00Z",
    padding: int = 0,
) -> bytes:
    unsigned = {
        "expires_at": expires_at,
        "issued_at": issued_at,
        "key_id": key_id,
        "license_id": f"lic-{key_id}",
        "machine_id": machine_id,
        "signature_alg": "ed25519",
    }
    if padding:
        unsigned["padding"] = "x" * padding
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
            str(old_trust),
        ],
        check=True,
    )
    live_license = tmp_path / "live" / "license.json"
    live_license.parent.mkdir()
    live_license.parent.chmod(0o755)
    live_license.write_bytes(_license(old_key, "license-old"))
    live_license.chmod(0o640)
    kwargs = {
        "trust_root": trust_root,
        "license_path": live_license,
        "state_root": tmp_path / "state",
        "lock_path": tmp_path / "release" / "release.lock",
        "expected_uid": os.getuid(),
        "trust_parent_mode": 0o700,
        "machine_id": "machine-1",
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


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (
            {"issued_at": "2099-01-01T00:00:00Z", "expires_at": "2100-01-01T00:00:00Z"},
            "not_yet_valid",
        ),
        (
            {"issued_at": "2020-01-01T00:00:00Z", "expires_at": "2021-01-01T00:00:00Z"},
            "expired",
        ),
        ({"machine_id": "other-machine"}, "machine_mismatch"),
    ],
)
def test_candidate_reuses_runtime_license_semantics(
    tmp_path: Path, candidate: dict[str, str], reason: str
):
    kwargs, old_key, new_key, new_trust, candidate_license = _fixture(tmp_path)
    candidate_license.write_bytes(_license(new_key, "license-new", **candidate))

    with pytest.raises(ValueError, match=reason):
        TRANSACTION.LicenseTransaction(**kwargs).activate(
            new_trust, candidate_license, "license-old"
        )

    _assert_live(
        kwargs,
        _license(old_key, "license-old"),
        candidate_license.read_bytes(),
    )
    assert not (kwargs["state_root"] / "journal.json").exists()


def test_maximum_size_pair_rollback_crash_journal_is_recoverable(tmp_path: Path):
    kwargs, old_key, new_key, new_trust, candidate_license = _fixture(tmp_path)
    padding = TRANSACTION.MAX_LICENSE_BYTES - 2048
    old_license = _license(old_key, "license-old", padding=padding)
    new_license = _license(new_key, "license-new", padding=padding)
    assert TRANSACTION.MAX_LICENSE_BYTES - 4096 < len(old_license) <= TRANSACTION.MAX_LICENSE_BYTES
    assert TRANSACTION.MAX_LICENSE_BYTES - 4096 < len(new_license) <= TRANSACTION.MAX_LICENSE_BYTES
    kwargs["license_path"].write_bytes(old_license)
    candidate_license.write_bytes(new_license)
    TRANSACTION.LicenseTransaction(**kwargs).activate(
        new_trust, candidate_license, "license-old"
    )

    def crash(point: str) -> None:
        if point == "after_state":
            raise TRANSACTION.InjectedCrash(point)

    with pytest.raises(TRANSACTION.InjectedCrash):
        TRANSACTION.LicenseTransaction(**kwargs, fault=crash).rollback("license-new")

    journal = kwargs["state_root"] / "journal.json"
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert set(payload) == {
        "operation",
        "phase",
        "schema",
        "source_policy",
        "source_state",
    }
    assert journal.stat().st_size < TRANSACTION.MAX_STATE_BYTES
    assert TRANSACTION.LicenseTransaction(**kwargs).recover() is True
    _assert_live(kwargs, new_license, old_license)


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


def test_state_and_lock_paths_reject_symlink_or_hardlink_attacks(tmp_path: Path):
    kwargs, _old_key, _new_key, _new_trust, _candidate_license = _fixture(tmp_path)
    attacker_state = tmp_path / "attacker-state"
    attacker_state.mkdir(mode=0o700)
    kwargs["state_root"].symlink_to(attacker_state, target_is_directory=True)
    with pytest.raises(ValueError, match="must be a directory"):
        TRANSACTION.LicenseTransaction(**kwargs).recover()
    kwargs["state_root"].unlink()

    attacker_lock_parent = tmp_path / "attacker-release"
    attacker_lock_parent.mkdir(mode=0o700)
    kwargs["lock_path"].parent.symlink_to(
        attacker_lock_parent, target_is_directory=True
    )
    with pytest.raises(ValueError, match="must be a directory"):
        TRANSACTION.LicenseTransaction(**kwargs).recover()
    kwargs["lock_path"].parent.unlink()

    kwargs["lock_path"].parent.mkdir(mode=0o700)
    kwargs["lock_path"].write_bytes(b"lock")
    kwargs["lock_path"].chmod(0o600)
    os.link(kwargs["lock_path"], tmp_path / "release-lock-alias")
    with pytest.raises(ValueError, match="lock ownership/mode"):
        TRANSACTION.LicenseTransaction(**kwargs).recover()


def test_candidate_files_reject_hardlinks_and_group_writes(tmp_path: Path):
    kwargs, _old_key, _new_key, new_trust, candidate_license = _fixture(tmp_path)
    trust_alias = tmp_path / "trust-alias.json"
    os.link(new_trust, trust_alias)
    with pytest.raises(ValueError, match="regular file"):
        TRANSACTION.LicenseTransaction(**kwargs).activate(
            trust_alias, candidate_license, "license-old"
        )
    trust_alias.unlink()
    new_trust.chmod(0o666)
    with pytest.raises(ValueError, match="mode is invalid"):
        TRANSACTION.LicenseTransaction(**kwargs).activate(
            new_trust, candidate_license, "license-old"
        )
    new_trust.chmod(0o644)
    candidate_license.chmod(0o660)
    with pytest.raises(ValueError, match="mode is invalid"):
        TRANSACTION.LicenseTransaction(**kwargs).activate(
            new_trust, candidate_license, "license-old"
        )


@pytest.mark.parametrize("live_name", ["policy", "license"])
def test_live_pair_rejects_hardlinks(tmp_path: Path, live_name: str):
    kwargs, _old_key, _new_key, new_trust, candidate_license = _fixture(tmp_path)
    live_path = (
        kwargs["trust_root"] / "policy.json"
        if live_name == "policy"
        else kwargs["license_path"]
    )
    os.link(live_path, tmp_path / f"{live_name}-alias")
    with pytest.raises(ValueError, match="regular file"):
        TRANSACTION.LicenseTransaction(**kwargs).activate(
            new_trust, candidate_license, "license-old"
        )


@pytest.mark.parametrize("attack", ["symlink", "hardlink"])
def test_existing_staged_trust_rejects_link_attacks(tmp_path: Path, attack: str):
    kwargs, _old_key, _new_key, new_trust, candidate_license = _fixture(tmp_path)
    target_dir = kwargs["trust_root"] / "versions" / "license-new"
    target_dir.mkdir(mode=0o755)
    target = target_dir / "license-trust.json"
    if attack == "symlink":
        target.symlink_to(new_trust)
    else:
        attacker = tmp_path / "attacker-trust.json"
        attacker.write_bytes(new_trust.read_bytes())
        attacker.chmod(0o444)
        os.link(attacker, target)
    with pytest.raises(ValueError, match="regular file"):
        TRANSACTION.LicenseTransaction(**kwargs).activate(
            new_trust, candidate_license, "license-old"
        )


def test_staging_cleanup_refuses_a_replaced_directory(tmp_path: Path):
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o755)
    expected = staging.lstat()
    moved = tmp_path / "moved"
    staging.rename(moved)
    staging.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="identity changed"):
        TRANSACTION.cleanup_staging_directory(staging, expected)


def test_production_cli_is_root_only_and_has_fixed_paths():
    source = TRANSACTION_PATH.read_text(encoding="utf-8")
    assert "os.geteuid() != 0" in source
    assert 'Path("/etc/drycontroller/keys/license-trust")' in source
    assert 'Path("/etc/drycontroller/license/license.json")' in source
    assert 'Path("/var/lib/drycontroller-ota/release.lock")' in source
    assert "--root" not in source
    transaction = TRANSACTION.production_transaction()
    assert transaction.trust_parent_mode == 0o755
    assert transaction.trust_root_mode == 0o755
    assert transaction.license_parent_mode == 0o755
    assert transaction.state_parent_mode == 0o700
    assert transaction.lock_parent_mode == 0o700


def test_boot_recovery_runs_before_docker_and_is_installed():
    unit = (
        ROOT / "infra/bootstrap/drycontroller-license-transaction-recovery.service"
    ).read_text(encoding="utf-8")
    installer = (ROOT / "infra/bootstrap/install-edge-host.sh").read_text(
        encoding="utf-8"
    )
    assert "Before=docker.service" in unit
    assert "RequiredBy=docker.service" in unit
    assert "WantedBy=multi-user.target" not in unit
    assert "manage-license-transaction.py recover" in unit
    assert "install -o root -g root -m 0755" in installer
    assert "/etc/drycontroller/license" in installer
    assert "/var/lib/drycontroller/license-transaction" in installer
    assert "systemctl enable drycontroller-license-transaction-recovery.service" in installer
