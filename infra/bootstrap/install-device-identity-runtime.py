#!/usr/bin/env python3
"""Create the missing device-identity runtime without touching the live stack."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
from typing import Callable


STATIC_INSTALLER = Path(__file__).with_name("install-static-runtime.py")
SPEC = importlib.util.spec_from_file_location(
    "drycontroller_static_installer", STATIC_INSTALLER
)
assert SPEC is not None and SPEC.loader is not None
static = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(static)

RuntimeInstallError = static.RuntimeInstallError
CommandRunner = Callable[[list[str], dict[str, str]], None]

DESTINATION = Path("/opt/drycontroller-identity")
STAGING_PREFIX = ".drycontroller-identity.install-"
MAX_WHEEL_BYTES = 128 * 1024 * 1024
MAX_WHEELHOUSE_BYTES = 512 * 1024 * 1024
WHEEL_LINE = re.compile(r"^([a-f0-9]{64}) [ *]\./([A-Za-z0-9_.+-]+\.whl)$")

RUNTIME_FILES = {
    "common/tailscale_status.py": ("common/tailscale_status.py", 0o644),
    "services/device-identity/requirements.txt": ("requirements.txt", 0o644),
    "services/device-identity/identity_service/__init__.py": (
        "identity_service/__init__.py",
        0o644,
    ),
    "services/device-identity/identity_service/api.py": (
        "identity_service/api.py",
        0o644,
    ),
    "services/device-identity/identity_service/backend.py": (
        "identity_service/backend.py",
        0o644,
    ),
    "services/device-identity/identity_service/contract.py": (
        "identity_service/contract.py",
        0o644,
    ),
    "services/device-identity/identity_service/host_authorization.py": (
        "identity_service/host_authorization.py",
        0o644,
    ),
    "services/device-identity/identity_service/rotation.py": (
        "identity_service/rotation.py",
        0o644,
    ),
    "services/device-identity/identity_service/state.py": (
        "identity_service/state.py",
        0o644,
    ),
    "services/device-identity/identity_service/tailnet.py": (
        "identity_service/tailnet.py",
        0o644,
    ),
}


def _sha256(path: Path, expected_uid: int) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != expected_uid
            or metadata.st_size > MAX_WHEEL_BYTES
            or metadata.st_mode & 0o022
        ):
            raise RuntimeInstallError(f"identity input is unsafe: {path.name}")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _verified_wheels(
    wheelhouse: Path, approved_manifest_sha256: str, expected_uid: int
) -> list[Path]:
    if not re.fullmatch(r"[a-f0-9]{64}", approved_manifest_sha256):
        raise RuntimeInstallError("approved identity wheelhouse checksum is invalid")
    try:
        metadata = wheelhouse.lstat()
    except OSError as exc:
        raise RuntimeInstallError("identity wheelhouse is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or metadata.st_mode & 0o022
    ):
        raise RuntimeInstallError("identity wheelhouse directory is unsafe")
    manifest = wheelhouse / "SHA256SUMS"
    if _sha256(manifest, expected_uid) != approved_manifest_sha256:
        raise RuntimeInstallError("identity wheelhouse manifest is not approved")
    try:
        lines = manifest.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeInstallError("identity wheelhouse manifest is invalid") from exc
    entries: list[tuple[str, str]] = []
    for line in lines:
        match = WHEEL_LINE.fullmatch(line)
        if match is None:
            raise RuntimeInstallError("identity wheelhouse manifest is invalid")
        entries.append((match.group(2), match.group(1)))
    if not entries or [name for name, _digest in entries] != sorted(
        name for name, _digest in entries
    ):
        raise RuntimeInstallError("identity wheelhouse manifest is not canonical")
    if len({name for name, _digest in entries}) != len(entries):
        raise RuntimeInstallError("identity wheelhouse manifest contains duplicates")
    observed_names = {path.name for path in wheelhouse.iterdir()}
    expected_names = {"SHA256SUMS", *(name for name, _digest in entries)}
    if observed_names != expected_names:
        raise RuntimeInstallError("identity wheelhouse file set is not exact")
    wheels: list[Path] = []
    total = 0
    for name, expected_digest in entries:
        wheel = wheelhouse / name
        if _sha256(wheel, expected_uid) != expected_digest:
            raise RuntimeInstallError(f"identity wheel checksum mismatch: {name}")
        total += wheel.stat().st_size
        wheels.append(wheel)
    if total > MAX_WHEELHOUSE_BYTES:
        raise RuntimeInstallError("identity wheelhouse is too large")
    return wheels


def _run_command(args: list[str], environment: dict[str, str]) -> None:
    try:
        completed = subprocess.run(
            args,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeInstallError("identity runtime command failed") from exc
    if completed.returncode != 0:
        raise RuntimeInstallError("identity runtime command failed")


def _relocate_venv_scripts(stage: Path, final: Path, expected_uid: int) -> None:
    bin_directory = stage / "venv/bin"
    old_prefix = str(stage).encode()
    new_prefix = str(final).encode()
    for script in bin_directory.iterdir():
        metadata = script.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            continue
        descriptor = os.open(script, os.O_RDWR | os.O_NOFOLLOW)
        try:
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_nlink != 1
                or observed.st_uid != expected_uid
                or observed.st_size > MAX_WHEEL_BYTES
                or observed.st_mode & 0o022
                or (observed.st_dev, observed.st_ino)
                != (metadata.st_dev, metadata.st_ino)
            ):
                raise RuntimeInstallError(
                    f"identity venv entry is unsafe: {script.name}"
                )
            content = b""
            while chunk := os.read(descriptor, 1024 * 1024):
                content += chunk
            if old_prefix not in content:
                continue
            relocated = content.replace(old_prefix, new_prefix)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.ftruncate(descriptor, 0)
            view = memoryview(relocated)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    uvicorn = bin_directory / "uvicorn"
    try:
        shebang = uvicorn.read_bytes().splitlines()[0]
    except (IndexError, OSError) as exc:
        raise RuntimeInstallError("identity uvicorn entrypoint is unavailable") from exc
    expected_shebang = f"#!{final}/venv/bin/python".encode()
    if not shebang.startswith(expected_shebang) or old_prefix in shebang:
        raise RuntimeInstallError("identity uvicorn entrypoint is not relocatable")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def install_device_identity_runtime(
    *,
    repo_source: Path,
    wheelhouse: Path,
    approved_wheelhouse_manifest_sha256: str,
    root: Path = Path("/"),
    owner_uid: int = 0,
    owner_gid: int = 0,
    command_runner: CommandRunner = _run_command,
) -> None:
    repo_source = Path(os.path.abspath(repo_source))
    wheelhouse = Path(os.path.abspath(wheelhouse))
    root = Path(os.path.abspath(root))
    wheels = _verified_wheels(
        wheelhouse, approved_wheelhouse_manifest_sha256, owner_uid
    )
    try:
        source_metadata = repo_source.lstat()
    except OSError as exc:
        raise RuntimeInstallError("device identity source is unavailable") from exc
    if (
        not stat.S_ISDIR(source_metadata.st_mode)
        or stat.S_ISLNK(source_metadata.st_mode)
        or source_metadata.st_uid != owner_uid
        or source_metadata.st_mode & 0o022
    ):
        raise RuntimeInstallError("device identity source directory is unsafe")
    for relative_source in RUNTIME_FILES:
        source = repo_source / relative_source
        static._validate_source(source)
        _sha256(source, owner_uid)

    root_fd = static._open_root(root, owner_uid, owner_gid)
    stage_name = f"{STAGING_PREFIX}{os.getpid()}-{secrets.token_hex(4)}"
    stage_absolute = f"/opt/{stage_name}"
    final = root / DESTINATION.relative_to("/")
    stage = root / stage_absolute.removeprefix("/")
    renamed_identity: tuple[int, int] | None = None
    try:
        if (root / "opt").exists():
            static._validate_destination(
                root_fd,
                "/opt",
                directory=True,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
        else:
            static._ensure_directory(root_fd, "/opt", 0o755, owner_uid, owner_gid)
        if final.exists() or final.is_symlink():
            raise RuntimeInstallError("device identity runtime already exists")
        opt = root / "opt"
        if any(path.name.startswith(STAGING_PREFIX) for path in opt.iterdir()):
            raise RuntimeInstallError(
                "stale device identity runtime staging path exists"
            )
        for absolute_path in (
            stage_absolute,
            f"{stage_absolute}/common",
            f"{stage_absolute}/identity_service",
            f"{stage_absolute}/wheelhouse",
        ):
            static._ensure_directory(
                root_fd, absolute_path, 0o755, owner_uid, owner_gid
            )
        for relative_source, (relative_destination, mode) in RUNTIME_FILES.items():
            static._install_file(
                repo_source / relative_source,
                root_fd,
                f"{stage_absolute}/{relative_destination}",
                mode,
                owner_uid,
                owner_gid,
            )
        for wheel in wheels:
            static._install_file(
                wheel,
                root_fd,
                f"{stage_absolute}/wheelhouse/{wheel.name}",
                0o444,
                owner_uid,
                owner_gid,
            )

        environment = {
            "HOME": "/root",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PIP_CONFIG_FILE": "/dev/null",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
        python = stage / "venv/bin/python"
        command_runner(
            ["/usr/bin/python3", "-m", "venv", str(stage / "venv")], environment
        )
        command_runner(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(stage / "wheelhouse"),
                "-r",
                str(stage / "requirements.txt"),
            ],
            environment,
        )
        command_runner([str(python), "-m", "pip", "check"], environment)
        if not python.exists() or not os.access(python, os.X_OK):
            raise RuntimeInstallError("device identity Python runtime is incomplete")
        _relocate_venv_scripts(stage, final, owner_uid)
        shutil.rmtree(stage / "wheelhouse")
        os.sync()
        stage_metadata = stage.lstat()
        renamed_identity = (stage_metadata.st_dev, stage_metadata.st_ino)
        os.rename(stage, final)
        final_python = final / "venv/bin/python"
        command_runner(
            [
                str(final_python),
                "-c",
                "from identity_service.backend import TpmIdentityBackend",
            ],
            {**environment, "PYTHONPATH": str(final)},
        )
        command_runner([str(final / "venv/bin/uvicorn"), "--version"], environment)
        _fsync_directory(opt)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        elif renamed_identity is not None:
            try:
                final_metadata = final.lstat()
            except FileNotFoundError:
                pass
            else:
                if (
                    stat.S_ISDIR(final_metadata.st_mode)
                    and not stat.S_ISLNK(final_metadata.st_mode)
                    and (final_metadata.st_dev, final_metadata.st_ino)
                    == renamed_identity
                ):
                    shutil.rmtree(final)
                    try:
                        _fsync_directory(opt)
                    except OSError:
                        pass
        raise
    finally:
        os.close(root_fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-source", required=True, type=Path)
    parser.add_argument("--wheelhouse", required=True, type=Path)
    parser.add_argument("--approved-wheelhouse-manifest-sha256", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("device identity runtime installation requires root")
    if os.uname().sysname != "Linux" or os.uname().machine != "x86_64":
        raise SystemExit("device identity runtime installation requires Linux amd64")
    try:
        install_device_identity_runtime(**vars(args))
    except (OSError, RuntimeInstallError) as exc:
        raise SystemExit(f"device identity runtime installation failed: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
