#!/usr/bin/env python3
"""Install only the fixed edge-app-v1 host runtime with safe path handling."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
from pathlib import Path


STATIC_INSTALLER = Path(__file__).with_name("install-static-runtime.py")
SPEC = importlib.util.spec_from_file_location("drycontroller_static_installer", STATIC_INSTALLER)
assert SPEC is not None and SPEC.loader is not None
static = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(static)

RuntimeInstallError = static.RuntimeInstallError

DIRECTORIES = {
    "/usr/libexec/drycontroller": 0o755,
    "/usr/libexec/drycontroller/common": 0o755,
    "/etc/drycontroller/keys": 0o755,
    "/etc/drycontroller/license": 0o755,
    "/var/lib/drycontroller": 0o700,
    "/var/lib/drycontroller/edge-daq": 0o700,
    "/var/lib/drycontroller/edge-daq/data": 0o700,
    "/var/lib/drycontroller-ota": 0o700,
    "/var/lib/drycontroller/license-transaction": 0o700,
    "/var/lib/drycontroller/cloud-request-nonces": 0o700,
    "/usr/local/bin": 0o755,
    "/etc/systemd/system": 0o755,
}

RUNTIME_FILES = {
    "infra/ota-helper/edge_app_release.py": (
        "/usr/libexec/drycontroller/edge_app_release.py",
        0o644,
    ),
    "infra/ota-helper/edge_app_ota_helper.py": (
        "/usr/libexec/drycontroller/drycontroller_ota_helper.py",
        0o644,
    ),
    "infra/ota-helper/edge-app-ota-apply": ("/usr/libexec/drycontroller/ota-apply", 0o755),
    "infra/ota-helper/initialize-edge-app-release": (
        "/usr/libexec/drycontroller/initialize-active-release",
        0o755,
    ),
    "infra/ota-helper/start-edge-app-release": (
        "/usr/libexec/drycontroller/start-active-release",
        0o755,
    ),
    "infra/ota-helper/rollback-edge-app": (
        "/usr/libexec/drycontroller/rollback-edge-app",
        0o755,
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
    "scripts/observe-tpm-identity.py": (
        "/usr/libexec/drycontroller/observe-tpm-identity.py",
        0o755,
    ),
    "scripts/observe-tailnet-status.py": (
        "/usr/libexec/drycontroller/observe-tailnet-status.py",
        0o755,
    ),
    "scripts/create-edge-app-baseline.py": (
        "/usr/libexec/drycontroller/create-edge-app-baseline.py",
        0o755,
    ),
    "infra/ota-helper/drycontroller-edge-app-ota-helper.service": (
        "/etc/systemd/system/drycontroller-ota-helper.service",
        0o644,
    ),
    "infra/ota-helper/drycontroller-edge-app-recovery.service": (
        "/etc/systemd/system/drycontroller-edge-app-recovery.service",
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
    "infra/bootstrap/drycontroller-tpm-identity-observer.service": (
        "/etc/systemd/system/drycontroller-tpm-identity-observer.service",
        0o644,
    ),
    "infra/bootstrap/drycontroller-tailnet-observer.service": (
        "/etc/systemd/system/drycontroller-tailnet-observer.service",
        0o644,
    ),
    "infra/bootstrap/drycontroller-tailnet-observer.timer": (
        "/etc/systemd/system/drycontroller-tailnet-observer.timer",
        0o644,
    ),
}


def _approved_sha256(path: Path, expected: str, label: str) -> None:
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise RuntimeInstallError(f"approved {label} checksum invalid")
    if static._secure_sha256(path) != expected:
        raise RuntimeInstallError(f"{label} checksum is not approved")


def install_edge_app_runtime(
    *,
    repo_source: Path,
    software_cosign_public_key: Path,
    approved_software_public_key_sha256: str,
    cosign_binary: Path,
    approved_cosign_sha256: str,
    root: Path = Path("/"),
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> None:
    repo_source = repo_source.resolve(strict=True)
    root = Path(os.path.abspath(root))
    _approved_sha256(cosign_binary, approved_cosign_sha256, "cosign")
    _approved_sha256(
        software_cosign_public_key,
        approved_software_public_key_sha256,
        "software public key",
    )
    inputs = [
        *(repo_source / relative_source for relative_source in RUNTIME_FILES),
        software_cosign_public_key,
        cosign_binary,
    ]
    for source in inputs:
        static._validate_source(source)
    external_files = (
        (software_cosign_public_key, "/etc/drycontroller/keys/cosign.pub", 0o644),
        (cosign_binary, "/usr/local/bin/cosign", 0o755),
    )

    root_fd = static._open_root(root, owner_uid, owner_gid)
    try:
        for absolute_path in DIRECTORIES:
            static._validate_destination(
                root_fd,
                absolute_path,
                directory=True,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
        destinations = [value for value in RUNTIME_FILES.values()]
        destinations.extend((destination, mode) for _, destination, mode in external_files)
        for absolute_destination, _mode in destinations:
            static._validate_destination(
                root_fd,
                absolute_destination,
                directory=False,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
        for absolute_path, mode in DIRECTORIES.items():
            static._ensure_directory(root_fd, absolute_path, mode, owner_uid, owner_gid)
        for relative_source, (absolute_destination, mode) in RUNTIME_FILES.items():
            static._install_file(
                repo_source / relative_source,
                root_fd,
                absolute_destination,
                mode,
                owner_uid,
                owner_gid,
            )
        for source, absolute_destination, mode in external_files:
            static._install_file(
                source,
                root_fd,
                absolute_destination,
                mode,
                owner_uid,
                owner_gid,
            )
    finally:
        os.close(root_fd)

    installed = {
        "cosign": ("/usr/local/bin/cosign", approved_cosign_sha256),
        "software public key": (
            "/etc/drycontroller/keys/cosign.pub",
            approved_software_public_key_sha256,
        ),
    }
    for label, (destination, expected) in installed.items():
        observed = hashlib.sha256(static._destination(root, destination).read_bytes()).hexdigest()
        if observed != expected:
            raise RuntimeInstallError(f"installed {label} checksum changed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-source", required=True, type=Path)
    parser.add_argument("--software-cosign-public-key", required=True, type=Path)
    parser.add_argument("--approved-software-public-key-sha256", required=True)
    parser.add_argument("--cosign-binary", required=True, type=Path)
    parser.add_argument("--approved-cosign-sha256", required=True)
    args = parser.parse_args()
    install_edge_app_runtime(**vars(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
