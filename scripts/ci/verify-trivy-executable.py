#!/usr/bin/env python3
"""Establish and verify the formal release Trivy executable identity."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path
from typing import Any


TRIVY_BINARY = Path("/usr/local/bin/trivy")
TRIVY_VERSION = "0.70.0"
TRIVY_BINARY_SHA256 = "379d59f24a4a828c55de5f0b91b6805cc35d13580180b658820e648611256166"
TRIVY_REQUIRED_UID = 0
TRIVY_REQUIRED_GID = 0
TRIVY_REQUIRED_MODE = 0o755


def _metadata():
    if not TRIVY_BINARY.is_absolute():
        raise ValueError("Trivy binary path must be absolute")
    try:
        metadata = TRIVY_BINARY.lstat()
    except FileNotFoundError as exc:
        raise ValueError("trusted Trivy binary is missing") from exc
    if not stat.S_ISREG(metadata.st_mode) or TRIVY_BINARY.is_symlink():
        raise ValueError("trusted Trivy binary must be a regular file and not a symlink")
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        metadata.st_uid != TRIVY_REQUIRED_UID
        or metadata.st_gid != TRIVY_REQUIRED_GID
        or mode != TRIVY_REQUIRED_MODE
    ):
        raise ValueError("trusted Trivy binary owner or mode is not approved")
    return metadata


def _verify_digest() -> tuple[str, int]:
    data = TRIVY_BINARY.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != TRIVY_BINARY_SHA256:
        raise ValueError("Trivy binary SHA-256 is not approved")
    return actual, len(data)


def _verify_version() -> str:
    try:
        completed = subprocess.run(
            (str(TRIVY_BINARY), "--version"),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Trivy version check failed") from exc
    lines = completed.stdout.splitlines()
    if not lines or lines[0].strip() != f"Version: {TRIVY_VERSION}":
        raise ValueError("Trivy version is not approved")
    return TRIVY_VERSION


def verify_trivy_executable() -> dict[str, Any]:
    metadata = _metadata()
    sha256, size = _verify_digest()
    version = _verify_version()
    return {
        "gid": metadata.st_gid,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "path": str(TRIVY_BINARY),
        "sha256": sha256,
        "size_bytes": size,
        "uid": metadata.st_uid,
        "version": version,
    }


def main() -> int:
    try:
        identity = verify_trivy_executable()
    except ValueError as exc:
        raise SystemExit(f"Trivy executable identity rejected: {exc}") from exc
    print(json.dumps(identity, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
