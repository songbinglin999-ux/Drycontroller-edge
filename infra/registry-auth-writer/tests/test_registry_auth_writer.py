from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MODULE_DIR = ROOT / "infra" / "registry-auth-writer"
if str(MODULE_DIR) not in os.sys.path:
    os.sys.path.insert(0, str(MODULE_DIR))

from registry_auth_writer import RegistryAuthError, install_registry_auth  # noqa: E402


def _payload(secret: str = "private-value") -> dict[str, object]:
    return {
        "credential_id": "cred-1",
        "registry_host": "https://registry.example.test/",
        "username": "edge-pull",
        "secret": secret,
        "secret_fingerprint": "fp-1",
        "version": 3,
    }


def _destination(tmp_path: Path) -> Path:
    root = tmp_path / "ota"
    root.mkdir(mode=0o700)
    return root / "registry-auth.json"


def test_atomic_root_auth_update_replaces_the_complete_document(tmp_path: Path):
    destination = _destination(tmp_path)
    first = install_registry_auth(_payload("first-secret"), path=destination)
    first_inode = destination.stat().st_ino
    second = install_registry_auth(_payload("second-secret"), path=destination)

    assert first == second
    assert "secret" not in second
    assert destination.stat().st_ino != first_inode
    assert destination.stat().st_mode & 0o777 == 0o400
    assert destination.stat().st_nlink == 1
    payload = json.loads(destination.read_text(encoding="utf-8"))
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
        install_registry_auth(_payload("new-secret"), path=destination)

    assert "new-secret" not in str(caught.value)
    assert destination.read_bytes() == before
    assert not list(destination.parent.glob(".*.tmp"))
