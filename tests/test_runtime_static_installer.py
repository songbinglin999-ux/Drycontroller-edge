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


def _inputs(tmp_path: Path) -> dict[str, object]:
    software_key = tmp_path / "software.pub"
    cloud_key = tmp_path / "cloud.pub"
    cosign = tmp_path / "cosign"
    software_key.write_bytes(b"software-key")
    cloud_key.write_bytes(b"cloud-key")
    cosign.write_bytes(b"cosign-binary")
    return {
        "repo_source": ROOT,
        "software_cosign_public_key": software_key,
        "approved_software_public_key_sha256": hashlib.sha256(b"software-key").hexdigest(),
        "device_identity_cloud_public_key": cloud_key,
        "approved_device_identity_cloud_public_key_sha256": hashlib.sha256(b"cloud-key").hexdigest(),
        "cosign_binary": cosign,
        "approved_cosign_sha256": hashlib.sha256(cosign.read_bytes()).hexdigest(),
        "owner_uid": os.geteuid(),
        "owner_gid": os.getegid(),
    }


def test_static_runtime_installer_reproduces_product_paths_modes_and_bytes(tmp_path):
    module = _load_installer()
    keys = tmp_path / "keys"
    keys.mkdir()
    software_key = keys / "software.pub"
    cloud_key = keys / "cloud.pub"
    cosign = keys / "cosign"
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
        software_cosign_public_key=software_key,
        approved_software_public_key_sha256=hashlib.sha256(b"software-key").hexdigest(),
        device_identity_cloud_public_key=cloud_key,
        approved_device_identity_cloud_public_key_sha256=hashlib.sha256(b"cloud-key").hexdigest(),
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
            software_cosign_public_key=public_input,
            approved_software_public_key_sha256=hashlib.sha256(b"public-key").hexdigest(),
            device_identity_cloud_public_key=public_input,
            approved_device_identity_cloud_public_key_sha256=hashlib.sha256(b"public-key").hexdigest(),
            cosign_binary=cosign,
            approved_cosign_sha256="0" * 64,
            root=destination,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )

    assert list(destination.iterdir()) == []


def test_static_runtime_installer_rejects_directory_symlink_before_writing(tmp_path):
    module = _load_installer()
    destination = tmp_path / "root"
    destination.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (destination / "usr").symlink_to(outside, target_is_directory=True)

    with pytest.raises(module.RuntimeInstallError, match="directory component is unsafe"):
        module.install_static_runtime(**_inputs(tmp_path), root=destination)

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
def test_static_runtime_installer_rejects_unsafe_existing_destination(
    tmp_path, kind
):
    module = _load_installer()
    destination = tmp_path / "root"
    target_parent = destination / "usr/local/bin"
    target_parent.mkdir(parents=True)
    target = target_parent / "cosign"
    outside = tmp_path / "outside"
    outside.write_bytes(b"must-not-change")
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, target)
    else:
        target.mkdir()

    with pytest.raises(module.RuntimeInstallError, match="destination"):
        module.install_static_runtime(**_inputs(tmp_path), root=destination)

    assert outside.read_bytes() == b"must-not-change"
    assert not (destination / "usr/libexec/drycontroller/edge_app_release.py").exists()


def test_static_runtime_installer_rejects_root_symlink_escape(tmp_path):
    module = _load_installer()
    outside = tmp_path / "outside-root"
    outside.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(module.RuntimeInstallError, match="root must be a real directory"):
        module.install_static_runtime(**_inputs(tmp_path), root=root_link)

    assert list(outside.iterdir()) == []


def test_static_runtime_installer_rejects_wrong_type_in_directory_chain(tmp_path):
    module = _load_installer()
    destination = tmp_path / "root"
    destination.mkdir()
    (destination / "etc").write_bytes(b"not-a-directory")

    with pytest.raises(module.RuntimeInstallError, match="directory component is unsafe"):
        module.install_static_runtime(**_inputs(tmp_path), root=destination)


def test_static_runtime_installer_rejects_writable_directory_component(tmp_path):
    module = _load_installer()
    destination = tmp_path / "root"
    destination.mkdir()
    (destination / "usr").mkdir(mode=0o777)
    (destination / "usr").chmod(0o777)

    with pytest.raises(module.RuntimeInstallError, match="directory component is untrusted"):
        module.install_static_runtime(**_inputs(tmp_path), root=destination)
