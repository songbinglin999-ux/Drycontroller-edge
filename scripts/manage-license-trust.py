#!/usr/bin/env python3
"""Atomically select one canonical license-trust.v1 artifact."""

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
    load_license_trust_policy,
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


def require_expected(policy: dict[str, object], expected: str) -> dict[str, str]:
    active = policy["active"]
    assert isinstance(active, dict)
    if active["key_id"] != expected:
        raise ValueError(
            f"active license trust is {active['key_id']}, expected {expected}"
        )
    return active


def command_initialize(root: Path, artifact: Path) -> None:
    policy_path = root / "policy.json"
    if policy_path.exists():
        raise ValueError("license trust policy is already initialized")
    trust = stage_artifact(root, artifact)
    write_policy(root, binding(trust), None)


def command_rotate(root: Path, artifact: Path, expected: str) -> None:
    policy = load_license_trust_policy(root / "policy.json")
    active = require_expected(policy, expected)
    trust = stage_artifact(root, artifact)
    if trust.key_id == active["key_id"]:
        raise ValueError("new license trust key_id must differ from active key_id")
    write_policy(root, binding(trust), active)


def command_rollback(root: Path, expected: str) -> None:
    policy = load_license_trust_policy(root / "policy.json")
    active = require_expected(policy, expected)
    previous = policy["previous"]
    if not isinstance(previous, dict):
        raise ValueError("license trust rollback target is unavailable")
    write_policy(root, previous, active)


def command_revoke_previous(root: Path, expected: str) -> None:
    policy = load_license_trust_policy(root / "policy.json")
    active = require_expected(policy, expected)
    if policy["previous"] is None:
        raise ValueError("license trust previous key is already revoked")
    write_policy(root, active, None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/etc/drycontroller/keys/license-trust"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    initialize = subparsers.add_parser("initialize")
    initialize.add_argument("artifact", type=Path)
    rotate = subparsers.add_parser("rotate")
    rotate.add_argument("artifact", type=Path)
    rotate.add_argument("--expected-active", required=True)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--expected-active", required=True)
    revoke = subparsers.add_parser("revoke-previous")
    revoke.add_argument("--expected-active", required=True)
    subparsers.add_parser("status")
    args = parser.parse_args()
    try:
        if args.command == "initialize":
            command_initialize(args.root, args.artifact)
        elif args.command == "rotate":
            command_rotate(args.root, args.artifact, args.expected_active)
        elif args.command == "rollback":
            command_rollback(args.root, args.expected_active)
        elif args.command == "revoke-previous":
            command_revoke_previous(args.root, args.expected_active)
        else:
            print((args.root / "policy.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"license trust operation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
