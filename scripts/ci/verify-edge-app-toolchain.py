#!/usr/bin/env python3
"""Verify the fixed Linux amd64 release builder toolchain before secrets are written."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import subprocess
from pathlib import Path
from typing import Any


TOOLS = {
    "docker": {
        "path": Path("/usr/bin/docker"),
        "sha256": "94afb5edf9d3565bd339dd61bdb9b53247e8c28bce4d04f021b90f845918bd9e",
        "version": "29.4.1-1",
    },
    "cosign": {
        "path": Path("/usr/local/libexec/drycontroller/cosign"),
        "sha256": "4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71",
        "version": "v3.1.3",
    },
    "trivy": {
        "path": Path("/usr/local/bin/trivy"),
        "sha256": "379d59f24a4a828c55de5f0b91b6805cc35d13580180b658820e648611256166",
        "version": "0.70.0",
    },
}


def _output(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
    )
    return completed.stdout.strip()


def _trusted_ancestors(path: Path, owner_uid: int, owner_gid: int) -> None:
    current = path.parent
    while True:
        metadata = current.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != owner_uid
            or metadata.st_gid != owner_gid
            or metadata.st_mode & 0o022
        ):
            raise ValueError(f"toolchain executable ancestor is untrusted: {current}")
        if current == Path("/"):
            return
        current = current.parent


def _identity(
    name: str,
    contract: dict[str, Any],
    *,
    owner_uid: int,
    owner_gid: int,
) -> dict[str, str]:
    path = contract["path"]
    _trusted_ancestors(path, owner_uid, owner_gid)
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != owner_uid
        or before.st_gid != owner_gid
        or stat.S_IMODE(before.st_mode) != 0o755
        or before.st_nlink != 1
    ):
        raise ValueError(f"{name} executable owner or mode is not approved")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError(f"{name} executable changed during validation")
        digest_builder = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest_builder.update(chunk)
        digest = digest_builder.hexdigest()
    finally:
        os.close(descriptor)
    if digest != contract["sha256"]:
        raise ValueError(f"{name} executable SHA-256 is not approved")
    return {"path": str(path), "sha256": digest}


def verify_release_toolchain(*, owner_uid: int = 0, owner_gid: int = 0) -> dict[str, Any]:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("release builder must be Linux amd64")

    receipt: dict[str, Any] = {
        "platform": "linux/amd64",
        "schema": "drycontroller.release-builder-toolchain.v1",
        "tools": {},
    }
    for name, contract in TOOLS.items():
        receipt["tools"][name] = _identity(
            name,
            contract,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )

    docker = _output(
        [
            str(TOOLS["docker"]["path"]),
            "version",
            "--format",
            "{{.Client.Version}}|{{.Client.Os}}/{{.Client.Arch}}",
        ]
    )
    if docker != f'{TOOLS["docker"]["version"]}|linux/amd64':
        raise ValueError("docker version or platform is not approved")

    cosign = _output([str(TOOLS["cosign"]["path"]), "version"])
    if (
        re.search(rf"GitVersion:\s+{re.escape(TOOLS['cosign']['version'])}(?:\s|$)", cosign)
        is None
        or re.search(r"Platform:\s+linux/amd64(?:\s|$)", cosign) is None
    ):
        raise ValueError("cosign version or platform is not approved")

    trivy = _output([str(TOOLS["trivy"]["path"]), "--version"])
    if re.search(rf"Version:\s+{re.escape(TOOLS['trivy']['version'])}(?:\s|$)", trivy) is None:
        raise ValueError("trivy version is not approved")

    for name, contract in TOOLS.items():
        receipt["tools"][name]["version"] = str(contract["version"])
    return receipt


def main() -> int:
    print(json.dumps(verify_release_toolchain(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
