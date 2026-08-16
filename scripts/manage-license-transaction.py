#!/usr/bin/env python3
"""Atomically activate or roll back a license trust-policy/license pair."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
from typing import Any, Callable


for import_root in (Path(__file__).resolve().parents[1], Path(__file__).resolve().parent):
    if (import_root / "common" / "license_verifier.py").is_file():
        if str(import_root) not in sys.path:
            sys.path.insert(0, str(import_root))
        break

from common.license_verifier import (  # noqa: E402
    LICENSE_TRUST_POLICY_SCHEMA,
    LicenseTrust,
    load_active_license_trust,
    load_license_trust,
    load_license_trust_policy,
    verify_license,
)


STATE_SCHEMA = "drycontroller.license-transaction-state.v1"
JOURNAL_SCHEMA = "drycontroller.license-transaction-journal.v1"
MAX_LICENSE_BYTES = 256 * 1024
MAX_STATE_BYTES = 1024 * 1024


class InjectedCrash(BaseException):
    """Test-only process-death simulation; normal recovery handlers must not catch it."""


def canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_regular(path: Path, expected_uid: int, maximum_size: int) -> os.stat_result:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ValueError(f"path must be a regular file: {path}")
    if metadata.st_uid != expected_uid:
        raise ValueError(f"path owner is not the expected uid: {path}")
    if metadata.st_size <= 0 or metadata.st_size > maximum_size:
        raise ValueError(f"path size is invalid: {path}")
    return metadata


def atomic_write(
    path: Path,
    content: bytes,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
        )
        try:
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, mode)
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


def binding(trust: LicenseTrust) -> dict[str, str]:
    return {"canonical_sha256": trust.canonical_sha256, "key_id": trust.key_id}


def policy(active: dict[str, str], previous: dict[str, str] | None) -> dict[str, Any]:
    return {
        "active": active,
        "previous": previous,
        "schema": LICENSE_TRUST_POLICY_SCHEMA,
    }


def parse_license(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_LICENSE_BYTES:
        raise ValueError("license file size is invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("license file is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("license file must contain a JSON object")
    return payload


def slot(trust_binding: dict[str, str], raw: bytes) -> dict[str, str]:
    return {
        "canonical_sha256": trust_binding["canonical_sha256"],
        "key_id": trust_binding["key_id"],
        "license_b64": base64.b64encode(raw).decode("ascii"),
        "license_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }


def decode_slot(value: Any) -> tuple[dict[str, str], bytes]:
    if not isinstance(value, dict) or set(value) != {
        "canonical_sha256",
        "key_id",
        "license_b64",
        "license_sha256",
    }:
        raise ValueError("license transaction slot is invalid")
    binding_value = {
        "canonical_sha256": value.get("canonical_sha256"),
        "key_id": value.get("key_id"),
    }
    try:
        raw = base64.b64decode(value.get("license_b64", ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("license transaction slot encoding is invalid") from exc
    if value.get("license_sha256") != "sha256:" + hashlib.sha256(raw).hexdigest():
        raise ValueError("license transaction slot digest mismatch")
    parse_license(raw)
    return binding_value, raw


class LicenseTransaction:
    def __init__(
        self,
        *,
        trust_root: Path,
        license_path: Path,
        state_root: Path,
        lock_path: Path,
        expected_uid: int = 0,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.trust_root = trust_root
        self.policy_path = trust_root / "policy.json"
        self.license_path = license_path
        self.state_root = state_root
        self.state_path = state_root / "state.json"
        self.journal_path = state_root / "journal.json"
        self.lock_path = lock_path
        self.expected_uid = expected_uid
        self.fault = fault or (lambda _point: None)

    def _prepare_roots(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_root, 0o700)
        metadata = self.state_root.stat()
        if metadata.st_uid != self.expected_uid or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError("license transaction state root ownership/mode is invalid")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _lock(self) -> int:
        self._prepare_roots()
        descriptor = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ValueError("shared release transaction lock ownership/mode is invalid")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("another release transaction is running") from exc
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _read_object(self, path: Path, schema: str) -> dict[str, Any]:
        require_regular(path, self.expected_uid, MAX_STATE_BYTES)
        raw = path.read_bytes()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid transaction JSON: {path}") from exc
        if not isinstance(payload, dict) or payload.get("schema") != schema:
            raise ValueError(f"invalid transaction schema: {path}")
        if canonical_json(payload) != raw:
            raise ValueError(f"transaction JSON is not canonical: {path}")
        return payload

    def _write_state(self, state: dict[str, Any]) -> None:
        atomic_write(
            self.state_path,
            canonical_json(state),
            mode=0o600,
            uid=self.expected_uid,
            gid=self.state_root.stat().st_gid,
        )

    def _write_journal(self, journal: dict[str, Any], phase: str) -> None:
        journal["phase"] = phase
        atomic_write(
            self.journal_path,
            canonical_json(journal),
            mode=0o600,
            uid=self.expected_uid,
            gid=self.state_root.stat().st_gid,
        )

    def _write_policy(self, value: dict[str, Any]) -> None:
        metadata = require_regular(self.policy_path, self.expected_uid, 16 * 1024)
        atomic_write(
            self.policy_path,
            canonical_json(value),
            mode=stat.S_IMODE(metadata.st_mode),
            uid=metadata.st_uid,
            gid=metadata.st_gid,
        )

    def _write_license(self, raw: bytes) -> None:
        metadata = require_regular(self.license_path, self.expected_uid, MAX_LICENSE_BYTES)
        atomic_write(
            self.license_path,
            raw,
            mode=stat.S_IMODE(metadata.st_mode),
            uid=metadata.st_uid,
            gid=metadata.st_gid,
        )

    def _stage_trust(self, source: Path) -> LicenseTrust:
        require_regular(source, self.expected_uid, 16 * 1024)
        raw = source.read_bytes()
        trust = load_license_trust(source)
        versions = self.trust_root / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        target_dir = versions / trust.key_id
        target = target_dir / "license-trust.json"
        if target_dir.exists():
            if target_dir.is_symlink() or not target_dir.is_dir() or not target.is_file():
                raise ValueError("existing license trust version path is invalid")
            if target.read_bytes() != raw:
                raise ValueError("license trust key_id already exists with different bytes")
            return trust
        temporary = versions / f".{trust.key_id}.txn.{os.getpid()}"
        try:
            temporary.mkdir(mode=0o755)
            atomic_write(
                temporary / "license-trust.json",
                raw,
                mode=0o444,
                uid=self.expected_uid,
                gid=versions.stat().st_gid,
            )
            os.rename(temporary, target_dir)
            fsync_directory(versions)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return trust

    def _live_state(self) -> tuple[dict[str, Any], dict[str, Any]]:
        require_regular(self.policy_path, self.expected_uid, 16 * 1024)
        license_metadata = require_regular(
            self.license_path, self.expected_uid, MAX_LICENSE_BYTES
        )
        del license_metadata
        live_policy = load_license_trust_policy(self.policy_path)
        trust = load_active_license_trust(self.policy_path)
        raw = self.license_path.read_bytes()
        verify_license(parse_license(raw), trust)
        active = live_policy["active"]
        assert isinstance(active, dict)
        live = {
            "active": slot(active, raw),
            "previous": None,
            "schema": STATE_SCHEMA,
        }
        if self.state_path.exists():
            stored = self._read_object(self.state_path, STATE_SCHEMA)
            stored_binding, stored_raw = decode_slot(stored.get("active"))
            if stored_binding != active or stored_raw != raw:
                raise ValueError("transaction state does not match the live license pair")
            previous = stored.get("previous")
            if previous is not None:
                decode_slot(previous)
            live = stored
        return live_policy, live

    def _verify_live(self, expected: dict[str, str], expected_raw: bytes) -> None:
        trust = load_active_license_trust(self.policy_path)
        if binding(trust) != expected:
            raise ValueError("live trust policy does not match transaction target")
        raw = self.license_path.read_bytes()
        if raw != expected_raw:
            raise ValueError("live license bytes do not match transaction target")
        verify_license(parse_license(raw), trust)

    def _restore_source(self, journal: dict[str, Any]) -> None:
        if set(journal) != {
            "operation",
            "phase",
            "schema",
            "source_policy",
            "source_state",
            "target_policy",
            "target_state",
        }:
            raise ValueError("license transaction journal fields are invalid")
        source_policy = journal["source_policy"]
        source_state = journal["source_state"]
        if not isinstance(source_policy, dict) or not isinstance(source_state, dict):
            raise ValueError("license transaction journal source is invalid")
        source_binding, source_raw = decode_slot(source_state.get("active"))
        self._write_policy(source_policy)
        self._write_license(source_raw)
        self._verify_live(source_binding, source_raw)
        self._write_state(source_state)
        self.journal_path.unlink()
        fsync_directory(self.state_root)

    def _recover_locked(self) -> bool:
        if not self.journal_path.exists():
            return False
        journal = self._read_object(self.journal_path, JOURNAL_SCHEMA)
        self._restore_source(journal)
        return True

    def recover(self) -> bool:
        descriptor = self._lock()
        try:
            return self._recover_locked()
        finally:
            os.close(descriptor)

    def _transition(
        self,
        *,
        operation: str,
        source_policy: dict[str, Any],
        source_state: dict[str, Any],
        target_policy: dict[str, Any],
        target_state: dict[str, Any],
    ) -> None:
        target_binding, target_raw = decode_slot(target_state["active"])
        journal = {
            "operation": operation,
            "phase": "prepared",
            "schema": JOURNAL_SCHEMA,
            "source_policy": source_policy,
            "source_state": source_state,
            "target_policy": target_policy,
            "target_state": target_state,
        }
        self._write_journal(journal, "prepared")
        self.fault("after_prepared")
        try:
            self._write_policy(target_policy)
            self._write_journal(journal, "policy_switched")
            self.fault("after_policy")
            self._write_license(target_raw)
            self._write_journal(journal, "license_switched")
            self.fault("after_license")
            self._verify_live(target_binding, target_raw)
            self._write_journal(journal, "verified")
            self.fault("after_verified")
            self._write_state(target_state)
            self._write_journal(journal, "state_committed")
            self.fault("after_state")
            self.journal_path.unlink()
            fsync_directory(self.state_root)
        except Exception:
            self._restore_source(journal)
            raise

    def activate(self, trust_source: Path, license_source: Path, expected_active: str) -> None:
        descriptor = self._lock()
        try:
            self._recover_locked()
            source_policy, source_state = self._live_state()
            active = source_policy["active"]
            assert isinstance(active, dict)
            if active["key_id"] != expected_active:
                raise ValueError(
                    f"active license trust is {active['key_id']}, expected {expected_active}"
                )
            trust = self._stage_trust(trust_source)
            if trust.key_id == active["key_id"]:
                raise ValueError("new license trust key_id must differ from active key_id")
            require_regular(license_source, self.expected_uid, MAX_LICENSE_BYTES)
            candidate_raw = license_source.read_bytes()
            verify_license(parse_license(candidate_raw), trust)
            target_binding = binding(trust)
            target_state = {
                "active": slot(target_binding, candidate_raw),
                "previous": source_state["active"],
                "schema": STATE_SCHEMA,
            }
            self._transition(
                operation="activate",
                source_policy=source_policy,
                source_state=source_state,
                target_policy=policy(target_binding, active),
                target_state=target_state,
            )
        finally:
            os.close(descriptor)

    def rollback(self, expected_active: str) -> None:
        descriptor = self._lock()
        try:
            self._recover_locked()
            source_policy, source_state = self._live_state()
            active = source_policy["active"]
            assert isinstance(active, dict)
            if active["key_id"] != expected_active:
                raise ValueError(
                    f"active license trust is {active['key_id']}, expected {expected_active}"
                )
            previous = source_state.get("previous")
            if previous is None:
                raise ValueError("paired license rollback target is unavailable")
            previous_binding, _ = decode_slot(previous)
            target_state = {
                "active": previous,
                "previous": source_state["active"],
                "schema": STATE_SCHEMA,
            }
            self._transition(
                operation="rollback",
                source_policy=source_policy,
                source_state=source_state,
                target_policy=policy(previous_binding, active),
                target_state=target_state,
            )
        finally:
            os.close(descriptor)


def production_transaction() -> LicenseTransaction:
    return LicenseTransaction(
        trust_root=Path("/etc/drycontroller/keys/license-trust"),
        license_path=Path("/etc/drycontroller/license/license.json"),
        state_root=Path("/var/lib/drycontroller/license-transaction"),
        lock_path=Path("/var/lib/drycontroller-ota/release.lock"),
    )


def main() -> int:
    if os.geteuid() != 0:
        print("license transaction must run as root", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    activate = subparsers.add_parser("activate")
    activate.add_argument("trust", type=Path)
    activate.add_argument("license", type=Path)
    activate.add_argument("--expected-active", required=True)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--expected-active", required=True)
    subparsers.add_parser("recover")
    args = parser.parse_args()
    transaction = production_transaction()
    try:
        if args.command == "activate":
            transaction.activate(args.trust, args.license, args.expected_active)
        elif args.command == "rollback":
            transaction.rollback(args.expected_active)
        else:
            transaction.recover()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"license transaction failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
