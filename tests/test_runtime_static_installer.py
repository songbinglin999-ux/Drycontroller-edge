from __future__ import annotations

import importlib.util
import hashlib
import os
import stat
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "infra" / "bootstrap" / "install-static-runtime.py"


def test_static_runtime_installer_is_importable():
    spec = importlib.util.spec_from_file_location("install_static_runtime", INSTALLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert callable(module.install_static_runtime)


def _load_installer():
    spec = importlib.util.spec_from_file_location("install_static_runtime_behavior", INSTALLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_static_runtime_installer_reproduces_product_paths_modes_and_bytes(tmp_path):
    module = _load_installer()
    keys = tmp_path / "keys"
    keys.mkdir()
    host_key = keys / "host.pub"
    software_key = keys / "software.pub"
    cloud_key = keys / "cloud.pub"
    cosign = keys / "cosign"
    host_key.write_bytes(b"host-key")
    software_key.write_bytes(b"software-key")
    cloud_key.write_bytes(b"cloud-key")
    cosign.write_bytes(b"cosign-binary")
    destination = tmp_path / "root"
    destination.mkdir()
    retired = destination / "usr/libexec/drycontroller/manage-license-trust.py"
    retired.parent.mkdir(parents=True)
    retired.write_text("retired", encoding="utf-8")

    module.install_static_runtime(
        repo_source=ROOT,
        host_cosign_public_key=host_key,
        software_cosign_public_key=software_key,
        device_identity_cloud_public_key=cloud_key,
        cosign_binary=cosign,
        approved_cosign_sha256=hashlib.sha256(cosign.read_bytes()).hexdigest(),
        root=destination,
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
    )

    for absolute_path, mode in module.DIRECTORIES.items():
        installed = destination / absolute_path.removeprefix("/")
        assert installed.is_dir()
        assert stat.S_IMODE(installed.stat().st_mode) == mode
    for source, (absolute_destination, mode) in module.RUNTIME_FILES.items():
        installed = destination / absolute_destination.removeprefix("/")
        assert installed.read_bytes() == (ROOT / source).read_bytes()
        assert stat.S_IMODE(installed.stat().st_mode) == mode
    for installed, expected in (
        ("etc/drycontroller/keys/host-release-cosign.pub", b"host-key"),
        ("etc/drycontroller/keys/cosign.pub", b"software-key"),
        ("etc/drycontroller/trust/device-identity-cloud.pub", b"cloud-key"),
        ("usr/local/bin/cosign", b"cosign-binary"),
    ):
        assert (destination / installed).read_bytes() == expected
    assert not retired.exists()


def test_static_runtime_installer_fails_before_writing_on_unapproved_cosign(tmp_path):
    module = _load_installer()
    destination = tmp_path / "root"
    destination.mkdir()
    public_input = tmp_path / "public"
    public_input.write_bytes(b"public")
    cosign = tmp_path / "cosign"
    cosign.write_bytes(b"not-approved")

    with pytest.raises(module.RuntimeInstallError, match="cosign checksum is not approved"):
        module.install_static_runtime(
            repo_source=ROOT,
            host_cosign_public_key=public_input,
            software_cosign_public_key=public_input,
            device_identity_cloud_public_key=public_input,
            cosign_binary=cosign,
            approved_cosign_sha256="0" * 64,
            root=destination,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )

    assert list(destination.iterdir()) == []
