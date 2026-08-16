#!/usr/bin/env python3
"""Initialize one canonical license-trust.v1 policy on a blank host."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys


for import_root in (Path(__file__).resolve().parents[1], Path(__file__).resolve().parent):
    if (import_root / "common" / "license_verifier.py").is_file():
        if str(import_root) not in sys.path:
            sys.path.insert(0, str(import_root))
        break

from common.license_verifier import (  # noqa: E402
    LICENSE_TRUST_POLICY_SCHEMA,
    LicenseTrust,
    load_license_trust,
)


def canonical_json(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def binding(trust: LicenseTrust) -> dict[str, str]:
    return {"canonical_sha256": trust.canonical_sha256, "key_id": trust.key_id}


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def stage_artifact(root: Path, source: Path) -> LicenseTrust:
    raw = source.read_bytes()
    trust = load_license_trust(source)
    versions = root / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    target_dir = versions / trust.key_id
    target = target_dir / "license-trust.json"
    if target_dir.exists():
        if not target_dir.is_dir() or target_dir.is_symlink() or not target.is_file():
            raise ValueError("existing license trust version path is invalid")
        if target.read_bytes() != raw:
            raise ValueError("license trust key_id already exists with different bytes")
        return trust
    temporary = versions / f".{trust.key_id}.tmp.{os.getpid()}"
    try:
        temporary.mkdir(mode=0o755)
        artifact = temporary / "license-trust.json"
        descriptor = os.open(artifact, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
        fsync_directory(temporary)
        os.rename(temporary, target_dir)
        fsync_directory(versions)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return trust


def write_policy(root: Path, active: dict[str, str], previous: dict[str, str] | None) -> None:
    payload: dict[str, object] = {
        "active": active,
        "previous": previous,
        "schema": LICENSE_TRUST_POLICY_SCHEMA,
    }
    atomic_write(root / "policy.json", canonical_json(payload), 0o444)


def command_initialize(root: Path, artifact: Path) -> None:
    policy_path = root / "policy.json"
    if policy_path.exists():
        raise ValueError("license trust policy is already initialized")
    trust = stage_artifact(root, artifact)
    write_policy(root, binding(trust), None)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Initialize license trust once; later changes require the paired transaction"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/etc/drycontroller/keys/license-trust"),
    )
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    try:
        command_initialize(args.root, args.artifact)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"license trust operation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
