#!/usr/bin/env python3
"""Install the immutable Edge runtime files shared by host setup and acceptance."""

from __future__ import annotations

import argparse
import errno
import hashlib
import os
import secrets
import stat
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
    "infra/ota-helper/edge_app_release.py": (
        "/usr/libexec/drycontroller/edge_app_release.py",
        0o644,
    ),
    "infra/ota-helper/drycontroller_ota_helper.py": (
        "/usr/libexec/drycontroller/drycontroller_ota_helper.py",
        0o644,
    ),
    "infra/ota-helper/ota-apply": (
        "/usr/libexec/drycontroller/ota-apply",
        0o755,
    ),
    "infra/ota-helper/initialize-active-release": (
        "/usr/libexec/drycontroller/initialize-active-release",
        0o755,
    ),
    "infra/ota-helper/start-active-release": (
        "/usr/libexec/drycontroller/start-active-release",
        0o755,
    ),
    "infra/ota-helper/rollback-edge-app": (
        "/usr/libexec/drycontroller/rollback-edge-app",
        0o755,
    ),
    "infra/ota-helper/release_security_contract.py": (
        "/usr/libexec/drycontroller/release_security_contract.py",
        0o644,
    ),
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
    "scripts/observe-tpm-identity.py": (
        "/usr/libexec/drycontroller/observe-tpm-identity.py",
        0o755,
    ),
    "scripts/create-edge-app-baseline.py": (
        "/usr/libexec/drycontroller/create-edge-app-baseline.py",
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
}


class RuntimeInstallError(RuntimeError):
    pass


def _destination(root: Path, absolute_path: str) -> Path:
    if not root.is_absolute() or not absolute_path.startswith("/"):
        raise RuntimeInstallError("runtime installation paths must be absolute")
    return root / absolute_path.removeprefix("/")


def _parts(absolute_path: str) -> tuple[str, ...]:
    path = Path(absolute_path)
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeInstallError("runtime installation path invalid")
    return tuple(part for part in path.parts if part != "/")


def _open_root(root: Path, owner_uid: int, owner_gid: int) -> int:
    try:
        before = os.lstat(root)
        if (
            not stat.S_ISDIR(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or (before.st_uid, before.st_gid) != (owner_uid, owner_gid)
            or before.st_mode & 0o022
        ):
            raise RuntimeInstallError("runtime root must be a real directory")
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        after = os.fstat(fd)
    except OSError as exc:
        raise RuntimeInstallError("runtime root is not trustworthy") from exc
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(fd)
        raise RuntimeInstallError("runtime root changed during validation")
    return fd


def _open_directory(
    root_fd: int,
    parts: tuple[str, ...],
    *,
    create: bool,
    owner_uid: int,
    owner_gid: int,
) -> int | None:
    current = os.dup(root_fd)
    try:
        for part in parts:
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current,
                )
            except FileNotFoundError:
                if not create:
                    os.close(current)
                    return None
                os.mkdir(part, 0o755, dir_fd=current)
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current,
                )
                os.fchown(child, owner_uid, owner_gid)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise RuntimeInstallError(
                        f"runtime directory component is unsafe: {part}"
                    ) from exc
                raise
            child_metadata = os.fstat(child)
            if (
                not stat.S_ISDIR(child_metadata.st_mode)
                or (child_metadata.st_uid, child_metadata.st_gid)
                != (owner_uid, owner_gid)
                or child_metadata.st_mode & 0o022
            ):
                os.close(child)
                raise RuntimeInstallError(f"runtime directory component is untrusted: {part}")
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _validate_source(source: Path) -> None:
    try:
        before = os.lstat(source)
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        after = os.fstat(fd)
    except OSError as exc:
        raise RuntimeInstallError(f"runtime source missing or unsafe: {source}") from exc
    finally:
        if "fd" in locals():
            os.close(fd)
    if (
        not stat.S_ISREG(before.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
    ):
        raise RuntimeInstallError(f"runtime source is not a stable regular file: {source}")


def _validate_destination(
    root_fd: int,
    absolute_path: str,
    *,
    directory: bool,
    owner_uid: int,
    owner_gid: int,
) -> None:
    parts = _parts(absolute_path)
    parent = _open_directory(
        root_fd,
        parts[:-1] if not directory else parts,
        create=False,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
    )
    if parent is None:
        return
    try:
        if directory:
            return
        try:
            observed = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(observed.st_mode):
            raise RuntimeInstallError(f"runtime destination is not a regular file: {absolute_path}")
        if observed.st_nlink != 1:
            raise RuntimeInstallError(f"runtime destination is hard-linked: {absolute_path}")
        if (observed.st_uid, observed.st_gid) != (owner_uid, owner_gid):
            raise RuntimeInstallError(f"runtime destination owner is unexpected: {absolute_path}")
    finally:
        os.close(parent)


def _ensure_directory(
    root_fd: int, absolute_path: str, mode: int, owner_uid: int, owner_gid: int
) -> None:
    directory_fd = _open_directory(
        root_fd,
        _parts(absolute_path),
        create=True,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
    )
    assert directory_fd is not None
    try:
        os.fchown(directory_fd, owner_uid, owner_gid)
        os.fchmod(directory_fd, mode)
    finally:
        os.close(directory_fd)


def _install_file(
    source: Path,
    root_fd: int,
    absolute_destination: str,
    mode: int,
    owner_uid: int,
    owner_gid: int,
) -> None:
    _validate_source(source)
    parts = _parts(absolute_destination)
    parent_fd = _open_directory(
        root_fd,
        parts[:-1],
        create=False,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
    )
    if parent_fd is None:
        raise RuntimeInstallError(f"runtime destination parent missing: {absolute_destination}")
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    temporary = f".{parts[-1]}.install-{os.getpid()}-{secrets.token_hex(4)}"
    temporary_fd: int | None = None
    try:
        temporary_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        while chunk := os.read(source_fd, 1024 * 1024):
            view = memoryview(chunk)
            while view:
                written = os.write(temporary_fd, view)
                view = view[written:]
        os.fchown(temporary_fd, owner_uid, owner_gid)
        os.fchmod(temporary_fd, mode)
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        os.replace(
            temporary,
            parts[-1],
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
        installed = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(installed.st_mode)
            or installed.st_nlink != 1
            or (installed.st_uid, installed.st_gid) != (owner_uid, owner_gid)
            or stat.S_IMODE(installed.st_mode) != mode
        ):
            raise RuntimeInstallError(f"runtime destination postcondition failed: {absolute_destination}")
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(source_fd)
        os.close(parent_fd)


def _secure_sha256(source: Path) -> str:
    _validate_source(source)
    digest = hashlib.sha256()
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


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
    root = Path(os.path.abspath(root))
    if len(approved_cosign_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in approved_cosign_sha256
    ):
        raise RuntimeInstallError("approved cosign checksum invalid")
    actual_cosign_sha256 = _secure_sha256(cosign_binary)
    if actual_cosign_sha256 != approved_cosign_sha256:
        raise RuntimeInstallError("cosign checksum is not approved")

    inputs = [
        *(repo_source / relative_source for relative_source in RUNTIME_FILES),
        host_cosign_public_key,
        software_cosign_public_key,
        device_identity_cloud_public_key,
        cosign_binary,
    ]
    for source in inputs:
        _validate_source(source)
    external_files = (
        (host_cosign_public_key, "/etc/drycontroller/keys/host-release-cosign.pub"),
        (software_cosign_public_key, "/etc/drycontroller/keys/cosign.pub"),
        (
            device_identity_cloud_public_key,
            "/etc/drycontroller/trust/device-identity-cloud.pub",
        ),
    )
    root_fd = _open_root(root, owner_uid, owner_gid)
    try:
        for absolute_path in DIRECTORIES:
            _validate_destination(
                root_fd,
                absolute_path,
                directory=True,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
        for _, (absolute_destination, _) in RUNTIME_FILES.items():
            _validate_destination(
                root_fd,
                absolute_destination,
                directory=False,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
        for _, absolute_destination in external_files:
            _validate_destination(
                root_fd,
                absolute_destination,
                directory=False,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
        _validate_destination(
            root_fd,
            "/usr/local/bin/cosign",
            directory=False,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )

        for absolute_path, mode in DIRECTORIES.items():
            _ensure_directory(root_fd, absolute_path, mode, owner_uid, owner_gid)

        retired_parts = _parts("/usr/libexec/drycontroller/manage-license-trust.py")
        retired_parent = _open_directory(
            root_fd,
            retired_parts[:-1],
            create=False,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
        assert retired_parent is not None
        try:
            try:
                retired = os.stat(
                    retired_parts[-1], dir_fd=retired_parent, follow_symlinks=False
                )
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISDIR(retired.st_mode):
                    raise RuntimeInstallError("retired runtime path is a directory")
                os.unlink(retired_parts[-1], dir_fd=retired_parent)
        finally:
            os.close(retired_parent)

        for relative_source, (absolute_destination, mode) in RUNTIME_FILES.items():
            _install_file(
                repo_source / relative_source,
                root_fd,
                absolute_destination,
                mode,
                owner_uid,
                owner_gid,
            )

        for source, absolute_destination in external_files:
            _install_file(
                source,
                root_fd,
                absolute_destination,
                0o644,
                owner_uid,
                owner_gid,
            )
        _install_file(
            cosign_binary,
            root_fd,
            "/usr/local/bin/cosign",
            0o755,
            owner_uid,
            owner_gid,
        )
    finally:
        os.close(root_fd)

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
