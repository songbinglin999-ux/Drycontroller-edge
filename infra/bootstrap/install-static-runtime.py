#!/usr/bin/env python3
"""Install the immutable Edge runtime files shared by host setup and acceptance."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
from pathlib import Path


DIRECTORIES = {
    "/usr/libexec/drycontroller": 0o755,
    "/usr/libexec/drycontroller/common": 0o755,
    "/etc/drycontroller/keys": 0o755,
    "/etc/drycontroller/trust": 0o755,
    "/etc/drycontroller/license": 0o755,
    "/var/lib/drycontroller": 0o700,
    "/var/lib/drycontroller-host": 0o700,
    "/var/lib/drycontroller-ota": 0o700,
    "/var/lib/drycontroller/license-transaction": 0o700,
    "/var/lib/drycontroller/cloud-request-nonces": 0o700,
    "/usr/local/bin": 0o755,
    "/etc/systemd/system": 0o755,
}

RUNTIME_FILES = {
    "infra/host-release-runner/host_release_engine.py": (
        "/usr/libexec/drycontroller/host_release_engine.py",
        0o644,
    ),
    "infra/host-release-runner/host_release_runner.py": (
        "/usr/libexec/drycontroller/host_release_runner.py",
        0o644,
    ),
    "common/tailscale_status.py": (
        "/usr/libexec/drycontroller/common/tailscale_status.py",
        0o644,
    ),
    "common/license_verifier.py": (
        "/usr/libexec/drycontroller/common/license_verifier.py",
        0o644,
    ),
    "scripts/manage-license-transaction.py": (
        "/usr/libexec/drycontroller/manage-license-transaction.py",
        0o755,
    ),
    "infra/host-release-runner/host-release-runner": (
        "/usr/libexec/drycontroller/host-release-runner",
        0o755,
    ),
    "infra/host-release-runner/host-release-apply": (
        "/usr/libexec/drycontroller/host-release-apply",
        0o755,
    ),
    "infra/host-release-runner/initialize-initial-release": (
        "/usr/libexec/drycontroller/initialize-initial-release",
        0o755,
    ),
    "scripts/ci/assert-fresh-install-data-plane.sh": (
        "/usr/libexec/drycontroller/assert-fresh-install-data-plane",
        0o755,
    ),
    "infra/host-release-runner/drycontroller-host-release-runner.service": (
        "/etc/systemd/system/drycontroller-host-release-runner.service",
        0o644,
    ),
    "infra/ota-helper/drycontroller-ota-helper.service": (
        "/etc/systemd/system/drycontroller-ota-helper.service",
        0o644,
    ),
    "infra/registry-auth-writer/registry_auth_writer.py": (
        "/usr/libexec/drycontroller/registry_auth_writer.py",
        0o644,
    ),
    "infra/registry-auth-writer/drycontroller-registry-auth-writer.service": (
        "/etc/systemd/system/drycontroller-registry-auth-writer.service",
        0o644,
    ),
    "infra/bootstrap/drycontroller-license-transaction-recovery.service": (
        "/etc/systemd/system/drycontroller-license-transaction-recovery.service",
        0o644,
    ),
}


class RuntimeInstallError(RuntimeError):
    pass


def _destination(root: Path, absolute_path: str) -> Path:
    if not root.is_absolute() or not absolute_path.startswith("/"):
        raise RuntimeInstallError("runtime installation paths must be absolute")
    return root / absolute_path.removeprefix("/")


def _install_file(
    source: Path, destination: Path, mode: int, owner_uid: int, owner_gid: int
) -> None:
    if not source.is_file():
        raise RuntimeInstallError(f"runtime source missing: {source}")
    shutil.copyfile(source, destination)
    os.chown(destination, owner_uid, owner_gid)
    destination.chmod(mode)


def install_static_runtime(
    *,
    repo_source: Path,
    host_cosign_public_key: Path,
    software_cosign_public_key: Path,
    device_identity_cloud_public_key: Path,
    cosign_binary: Path,
    approved_cosign_sha256: str,
    root: Path = Path("/"),
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> None:
    """Install the static runtime with the exact production paths and modes."""

    repo_source = repo_source.resolve(strict=True)
    root = root.resolve(strict=True)
    if len(approved_cosign_sha256) != 64:
        raise RuntimeInstallError("approved cosign checksum invalid")
    actual_cosign_sha256 = hashlib.sha256(cosign_binary.read_bytes()).hexdigest()
    if actual_cosign_sha256 != approved_cosign_sha256:
        raise RuntimeInstallError("cosign checksum is not approved")

    for absolute_path, mode in DIRECTORIES.items():
        directory = _destination(root, absolute_path)
        directory.mkdir(parents=True, exist_ok=True)
        os.chown(directory, owner_uid, owner_gid)
        directory.chmod(mode)

    retired = _destination(root, "/usr/libexec/drycontroller/manage-license-trust.py")
    if retired.exists() or retired.is_symlink():
        retired.unlink()

    for relative_source, (absolute_destination, mode) in RUNTIME_FILES.items():
        _install_file(
            repo_source / relative_source,
            _destination(root, absolute_destination),
            mode,
            owner_uid,
            owner_gid,
        )

    for source, absolute_destination in (
        (host_cosign_public_key, "/etc/drycontroller/keys/host-release-cosign.pub"),
        (software_cosign_public_key, "/etc/drycontroller/keys/cosign.pub"),
        (
            device_identity_cloud_public_key,
            "/etc/drycontroller/trust/device-identity-cloud.pub",
        ),
    ):
        _install_file(
            source,
            _destination(root, absolute_destination),
            0o644,
            owner_uid,
            owner_gid,
        )
    _install_file(
        cosign_binary,
        _destination(root, "/usr/local/bin/cosign"),
        0o755,
        owner_uid,
        owner_gid,
    )

    installed_cosign_sha256 = hashlib.sha256(
        _destination(root, "/usr/local/bin/cosign").read_bytes()
    ).hexdigest()
    if installed_cosign_sha256 != approved_cosign_sha256:
        raise RuntimeInstallError("installed cosign checksum changed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-source", required=True, type=Path)
    parser.add_argument("--host-cosign-public-key", required=True, type=Path)
    parser.add_argument("--software-cosign-public-key", required=True, type=Path)
    parser.add_argument("--device-identity-cloud-public-key", required=True, type=Path)
    parser.add_argument("--cosign-binary", required=True, type=Path)
    parser.add_argument("--approved-cosign-sha256", required=True)
    args = parser.parse_args()
    install_static_runtime(
        repo_source=args.repo_source,
        host_cosign_public_key=args.host_cosign_public_key,
        software_cosign_public_key=args.software_cosign_public_key,
        device_identity_cloud_public_key=args.device_identity_cloud_public_key,
        cosign_binary=args.cosign_binary,
        approved_cosign_sha256=args.approved_cosign_sha256,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
