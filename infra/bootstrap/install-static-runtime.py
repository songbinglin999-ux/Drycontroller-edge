#!/usr/bin/env python3
"""Shared safe file-install primitives for the active runtime installers."""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
from pathlib import Path


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
