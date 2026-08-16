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
    license_runtime_error,
    load_active_license_trust,
    load_license_trust_policy,
    parse_license_trust,
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


def require_regular(
    path: Path,
    expected_uid: int,
    maximum_size: int,
    *,
    expected_modes: set[int] | None = None,
) -> os.stat_result:
    metadata = path.lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_nlink != 1
    ):
        raise ValueError(f"path must be a regular file: {path}")
    if metadata.st_uid != expected_uid:
        raise ValueError(f"path owner is not the expected uid: {path}")
    if mode & 0o022 or (expected_modes is not None and mode not in expected_modes):
        raise ValueError(f"path mode is invalid: {path}")
    if metadata.st_size <= 0 or metadata.st_size > maximum_size:
        raise ValueError(f"path size is invalid: {path}")
    return metadata


def read_regular(
    path: Path,
    expected_uid: int,
    maximum_size: int,
    *,
    expected_modes: set[int] | None = None,
) -> tuple[bytes, os.stat_result]:
    before = require_regular(
        path,
        expected_uid,
        maximum_size,
        expected_modes=expected_modes,
    )
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_nlink != 1
        ):
            raise ValueError(f"path changed while opening: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum_size + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_size:
                raise ValueError(f"path size is invalid: {path}")
        return b"".join(chunks), opened
    finally:
        os.close(descriptor)


def secure_directory(path: Path, expected_uid: int, expected_mode: int) -> os.stat_result:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"path must be a directory: {path}")
    if (
        metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != expected_mode
    ):
        raise ValueError(f"directory ownership/mode is invalid: {path}")
    return metadata


def cleanup_staging_directory(path: Path, expected: os.stat_result) -> None:
    try:
        actual = path.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISLNK(actual.st_mode)
        or not stat.S_ISDIR(actual.st_mode)
        or actual.st_dev != expected.st_dev
        or actual.st_ino != expected.st_ino
    ):
        raise ValueError("license trust staging directory identity changed")
    artifact = path / "license-trust.json"
    if artifact.exists():
        require_regular(artifact, expected.st_uid, 16 * 1024, expected_modes={0o444})
        artifact.unlink()
    os.rmdir(path)
    fsync_directory(path.parent)


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
        trust_parent_mode: int = 0o755,
        trust_root_mode: int = 0o755,
        license_parent_mode: int = 0o755,
        state_parent_mode: int = 0o700,
        lock_parent_mode: int = 0o700,
        machine_id: str = "",
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
        self.trust_parent_mode = trust_parent_mode
        self.trust_root_mode = trust_root_mode
        self.license_parent_mode = license_parent_mode
        self.state_parent_mode = state_parent_mode
        self.lock_parent_mode = lock_parent_mode
        self.machine_id = machine_id
        self.fault = fault or (lambda _point: None)

    def _prepare_roots(self) -> None:
        secure_directory(
            self.trust_root.parent, self.expected_uid, self.trust_parent_mode
        )
        secure_directory(self.trust_root, self.expected_uid, self.trust_root_mode)
        secure_directory(
            self.trust_root / "versions", self.expected_uid, self.trust_root_mode
        )
        secure_directory(
            self.license_path.parent, self.expected_uid, self.license_parent_mode
        )
        secure_directory(
            self.state_root.parent, self.expected_uid, self.state_parent_mode
        )
        try:
            self.state_root.lstat()
        except FileNotFoundError:
            self.state_root.mkdir(mode=0o700)
        secure_directory(self.state_root, self.expected_uid, 0o700)
        try:
            self.lock_path.parent.lstat()
        except FileNotFoundError:
            self.lock_path.parent.mkdir(mode=self.lock_parent_mode)
        secure_directory(
            self.lock_path.parent, self.expected_uid, self.lock_parent_mode
        )

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
                or metadata.st_nlink != 1
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
        raw, _metadata = read_regular(
            path,
            self.expected_uid,
            MAX_STATE_BYTES,
            expected_modes={0o600},
        )
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
        metadata = require_regular(
            self.policy_path,
            self.expected_uid,
            16 * 1024,
            expected_modes={0o444},
        )
        atomic_write(
            self.policy_path,
            canonical_json(value),
            mode=stat.S_IMODE(metadata.st_mode),
            uid=metadata.st_uid,
            gid=metadata.st_gid,
        )

    def _write_license(self, raw: bytes) -> None:
        metadata = require_regular(
            self.license_path,
            self.expected_uid,
            MAX_LICENSE_BYTES,
            expected_modes={0o640},
        )
        atomic_write(
            self.license_path,
            raw,
            mode=stat.S_IMODE(metadata.st_mode),
            uid=metadata.st_uid,
            gid=metadata.st_gid,
        )

    def _stage_trust(self, source: Path) -> LicenseTrust:
        raw, _source_metadata = read_regular(source, self.expected_uid, 16 * 1024)
        trust = parse_license_trust(raw)
        versions = self.trust_root / "versions"
        versions_metadata = secure_directory(
            versions, self.expected_uid, self.trust_root_mode
        )
        target_dir = versions / trust.key_id
        target = target_dir / "license-trust.json"
        try:
            target_dir.lstat()
        except FileNotFoundError:
            target_exists = False
        else:
            target_exists = True
        if target_exists:
            secure_directory(target_dir, self.expected_uid, self.trust_root_mode)
            existing, _metadata = read_regular(
                target,
                self.expected_uid,
                16 * 1024,
                expected_modes={0o444},
            )
            if existing != raw:
                raise ValueError("license trust key_id already exists with different bytes")
            return trust
        temporary = versions / f".{trust.key_id}.txn.{os.getpid()}"
        temporary_metadata: os.stat_result | None = None
        try:
            temporary.mkdir(mode=0o755)
            temporary_metadata = secure_directory(
                temporary, self.expected_uid, self.trust_root_mode
            )
            atomic_write(
                temporary / "license-trust.json",
                raw,
                mode=0o444,
                uid=self.expected_uid,
                gid=versions_metadata.st_gid,
            )
            os.rename(temporary, target_dir)
            fsync_directory(versions)
        finally:
            if temporary_metadata is not None:
                cleanup_staging_directory(temporary, temporary_metadata)
        return trust

    def _live_state(self) -> tuple[dict[str, Any], dict[str, Any]]:
        require_regular(
            self.policy_path,
            self.expected_uid,
            16 * 1024,
            expected_modes={0o444},
        )
        require_regular(
            self.license_path,
            self.expected_uid,
            MAX_LICENSE_BYTES,
            expected_modes={0o640},
        )
        live_policy = load_license_trust_policy(self.policy_path)
        trust = load_active_license_trust(self.policy_path)
        raw = self.license_path.read_bytes()
        self._verify_runtime(parse_license(raw), trust)
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
        self._verify_runtime(parse_license(raw), trust)

    def _verify_runtime(self, payload: dict[str, Any], trust: LicenseTrust) -> None:
        verify_license(payload, trust)
        if not self.machine_id:
            raise ValueError("current machine id is unavailable")
        runtime_error = license_runtime_error(payload, machine_id=self.machine_id)
        if runtime_error is not None:
            raise ValueError(f"license runtime validation failed: {runtime_error}")

    def _restore_source(self, journal: dict[str, Any]) -> None:
        if set(journal) != {
            "operation",
            "phase",
            "schema",
            "source_policy",
            "source_state",
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
            candidate_raw, _candidate_metadata = read_regular(
                license_source, self.expected_uid, MAX_LICENSE_BYTES
            )
            self._verify_runtime(parse_license(candidate_raw), trust)
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
    try:
        machine_id = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        machine_id = ""
    return LicenseTransaction(
        trust_root=Path("/etc/drycontroller/keys/license-trust"),
        license_path=Path("/etc/drycontroller/license/license.json"),
        state_root=Path("/var/lib/drycontroller/license-transaction"),
        lock_path=Path("/var/lib/drycontroller-ota/release.lock"),
        machine_id=machine_id,
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
