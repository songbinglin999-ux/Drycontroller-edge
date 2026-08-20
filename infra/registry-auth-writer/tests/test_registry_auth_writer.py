from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MODULE_DIR = ROOT / "infra" / "registry-auth-writer"
if str(MODULE_DIR) not in os.sys.path:
    os.sys.path.insert(0, str(MODULE_DIR))

from registry_auth_writer import (  # noqa: E402
    DOCUMENT_SCHEMA,
    RegistryAuthError,
    install_registry_auth,
    read_registry_status,
)


def _fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def _payload(secret: str = "private-value", *, version: int = 3) -> dict[str, object]:
    return {
        "credential_id": "cred-1",
        "registry_host": "https://registry.example.test/",
        "username": "edge-pull",
        "secret": secret,
        "secret_fingerprint": _fingerprint(secret),
        "version": version,
        "transaction_id": "a" * 32,
        "audit_id": 17,
    }


def _destination(tmp_path: Path) -> Path:
    root = tmp_path / "ota"
    root.mkdir(mode=0o700)
    return root / "registry-auth.json"


def test_atomic_root_auth_update_replaces_the_complete_document(tmp_path: Path):
    destination = _destination(tmp_path)
    first = install_registry_auth(_payload("first-secret"), path=destination)
    first_inode = destination.stat().st_ino
    second = install_registry_auth(_payload("second-secret", version=4), path=destination)

    assert first["credential_id"] == second["credential_id"] == "cred-1"
    assert first["transaction_id"] == second["transaction_id"] == "a" * 32
    assert first["audit_id"] == second["audit_id"] == 17
    assert "secret" not in second
    assert destination.stat().st_ino != first_inode
    assert destination.stat().st_mode & 0o777 == 0o400
    assert destination.stat().st_nlink == 1
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["schema"] == DOCUMENT_SCHEMA
    assert payload["credential"] == second
    assert read_registry_status(path=destination) == second
    encoded = payload["auths"]["registry.example.test"]["auth"]
    assert base64.b64decode(encoded).decode("utf-8") == "edge-pull:second-secret"
    assert not list(destination.parent.glob(".*.tmp"))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("secret"),
        lambda payload: payload.update(secret=""),
        lambda payload: payload.update(registry_host="../socket"),
        lambda payload: payload.update(version=0),
        lambda payload: payload.update(transaction_id="not-a-transaction"),
        lambda payload: payload.update(audit_id=0),
        lambda payload: payload.update(extra="value"),
    ],
)
def test_invalid_payload_never_changes_existing_auth(tmp_path: Path, mutation):
    destination = _destination(tmp_path)
    install_registry_auth(_payload("retained"), path=destination)
    before = destination.read_bytes()
    payload = _payload("attacker")
    mutation(payload)

    with pytest.raises(RegistryAuthError, match="payload invalid"):
        install_registry_auth(payload, path=destination)

    assert destination.read_bytes() == before


def test_linked_or_wrong_mode_destination_is_refused(tmp_path: Path):
    destination = _destination(tmp_path)
    other = tmp_path / "other"
    other.write_text("retained", encoding="utf-8")
    destination.symlink_to(other)

    with pytest.raises(RegistryAuthError, match="destination unavailable"):
        install_registry_auth(_payload(), path=destination)

    assert other.read_text(encoding="utf-8") == "retained"


def test_failed_atomic_replace_retains_previous_auth_and_removes_temporary_file(
    tmp_path: Path,
    monkeypatch,
):
    destination = _destination(tmp_path)
    install_registry_auth(_payload("retained"), path=destination)
    before = destination.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(RegistryAuthError, match="update failed") as caught:
        install_registry_auth(_payload("new-secret", version=4), path=destination)

    assert "new-secret" not in str(caught.value)
    assert destination.read_bytes() == before
    assert not list(destination.parent.glob(".*.tmp"))


def test_missing_status_is_explicit_and_contains_no_secret(tmp_path: Path):
    status = read_registry_status(path=_destination(tmp_path))

    assert status == {"status": "missing"}
    assert "secret" not in json.dumps(status)


def test_claimed_fingerprint_must_match_actual_secret_and_never_replace_active_auth(
    tmp_path: Path,
):
    destination = _destination(tmp_path)
    installed = install_registry_auth(_payload("old-secret", version=5), path=destination)
    before = destination.read_bytes()
    payload = _payload("different-secret", version=4)
    payload["secret_fingerprint"] = _fingerprint("old-secret")

    with pytest.raises(RegistryAuthError, match="payload invalid"):
        install_registry_auth(payload, path=destination)

    assert destination.read_bytes() == before
    assert read_registry_status(path=destination) == installed


def test_lower_version_cannot_replace_active_auth(tmp_path: Path):
    destination = _destination(tmp_path)
    installed = install_registry_auth(_payload("old-secret", version=5), path=destination)
    before = destination.read_bytes()

    with pytest.raises(RegistryAuthError, match="version rollback"):
        install_registry_auth(_payload("different-secret", version=4), path=destination)

    assert destination.read_bytes() == before
    assert read_registry_status(path=destination) == installed


def test_same_version_different_secret_is_refused(tmp_path: Path):
    destination = _destination(tmp_path)
    installed = install_registry_auth(_payload("old-secret", version=5), path=destination)
    before = destination.read_bytes()

    with pytest.raises(RegistryAuthError, match="version conflict"):
        install_registry_auth(_payload("different-secret", version=5), path=destination)

    assert destination.read_bytes() == before
    assert read_registry_status(path=destination) == installed


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("credential_id", "cred-2"),
        ("username", "other-reader"),
        ("transaction_id", "b" * 32),
        ("audit_id", 18),
    ],
)
def test_same_version_different_metadata_is_refused(tmp_path: Path, field: str, value: object):
    destination = _destination(tmp_path)
    payload = _payload("old-secret", version=5)
    installed = install_registry_auth(payload, path=destination)
    before = destination.read_bytes()
    replacement = _payload("old-secret", version=5)
    replacement[field] = value

    with pytest.raises(RegistryAuthError, match="version conflict"):
        install_registry_auth(replacement, path=destination)

    assert destination.read_bytes() == before
    assert read_registry_status(path=destination) == installed


def test_exact_same_version_request_is_idempotent(tmp_path: Path):
    destination = _destination(tmp_path)
    payload = _payload("same-secret", version=5)
    first = install_registry_auth(payload, path=destination)
    before = destination.read_bytes()
    inode = destination.stat().st_ino

    second = install_registry_auth(payload, path=destination)

    assert second == first
    assert destination.read_bytes() == before
    assert destination.stat().st_ino == inode


@pytest.mark.parametrize("tamper", ["auth", "fingerprint"])
def test_status_read_rejects_secret_fingerprint_tampering(tmp_path: Path, tamper: str):
    destination = _destination(tmp_path)
    install_registry_auth(_payload("old-secret", version=5), path=destination)
    document = json.loads(destination.read_text(encoding="utf-8"))
    if tamper == "auth":
        document["auths"]["registry.example.test"]["auth"] = base64.b64encode(
            b"edge-pull:different-secret"
        ).decode("ascii")
    else:
        document["credential"]["secret_fingerprint"] = _fingerprint("different-secret")
    destination.chmod(0o600)
    destination.write_text(json.dumps(document), encoding="utf-8")
    destination.chmod(0o400)

    with pytest.raises(RegistryAuthError, match="status unavailable"):
        read_registry_status(path=destination)
