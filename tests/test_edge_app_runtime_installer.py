from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import stat

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "infra/bootstrap/install-edge-app-runtime.py"


def _load_installer():
    spec = importlib.util.spec_from_file_location("edge_app_runtime_installer", INSTALLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inputs(tmp_path: Path) -> dict[str, object]:
    software_key = tmp_path / "software.pub"
    cosign = tmp_path / "cosign"
    software_key.write_bytes(b"software-key")
    cosign.write_bytes(b"cosign-binary")
    cosign.chmod(0o755)
    return {
        "repo_source": ROOT,
        "software_cosign_public_key": software_key,
        "approved_software_public_key_sha256": hashlib.sha256(b"software-key").hexdigest(),
        "cosign_binary": cosign,
        "approved_cosign_sha256": hashlib.sha256(b"cosign-binary").hexdigest(),
        "owner_uid": os.geteuid(),
        "owner_gid": os.getegid(),
    }


def test_installer_writes_only_edge_app_runtime_with_exact_modes(tmp_path: Path):
    module = _load_installer()
    destination = tmp_path / "root"
    destination.mkdir(mode=0o755)

    module.install_edge_app_runtime(**_inputs(tmp_path), root=destination)

    assert not any("host-release" in source for source in module.RUNTIME_FILES)
    for source, (absolute_destination, mode) in module.RUNTIME_FILES.items():
        installed = destination / absolute_destination.removeprefix("/")
        assert installed.read_bytes() == (ROOT / source).read_bytes()
        assert stat.S_IMODE(installed.stat().st_mode) == mode
    expected = {
        "etc/drycontroller/keys/cosign.pub": b"software-key",
        "usr/local/bin/cosign": b"cosign-binary",
    }
    for relative, content in expected.items():
        assert (destination / relative).read_bytes() == content


def test_installer_rejects_unapproved_public_key_before_writing(tmp_path: Path):
    module = _load_installer()
    destination = tmp_path / "root"
    destination.mkdir(mode=0o755)
    inputs = _inputs(tmp_path)
    inputs["approved_software_public_key_sha256"] = "0" * 64

    with pytest.raises(module.RuntimeInstallError, match="software public key checksum"):
        module.install_edge_app_runtime(**inputs, root=destination)

    assert list(destination.iterdir()) == []


def test_installer_rejects_destination_symlink_escape(tmp_path: Path):
    module = _load_installer()
    destination = tmp_path / "root"
    destination.mkdir(mode=0o755)
    outside = tmp_path / "outside"
    outside.mkdir()
    (destination / "usr").symlink_to(outside, target_is_directory=True)

    with pytest.raises(module.RuntimeInstallError):
        module.install_edge_app_runtime(**_inputs(tmp_path), root=destination)

    assert list(outside.iterdir()) == []
