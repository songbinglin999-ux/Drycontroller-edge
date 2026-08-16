#!/usr/bin/env python3
"""Create and verify stable descriptors for release artifacts."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import signal
import stat
from time import monotonic
from pathlib import Path


CHUNK_BYTES = 4 * 1024 * 1024


def _deadline_expired(_signum: int, _frame: object) -> None:
    raise TimeoutError("release artifact snapshot deadline exceeded")


def _check_deadline(deadline: float) -> None:
    if monotonic() >= deadline:
        raise TimeoutError("release artifact snapshot deadline exceeded")


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def create_snapshot(
    source: Path,
    destination: Path,
    *,
    maximum_size: int,
    timeout_seconds: int,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> tuple[str, int]:
    if maximum_size < 1:
        raise ValueError("maximum size is invalid")
    if timeout_seconds < 1:
        raise ValueError("snapshot timeout is invalid")
    if (expected_sha256 is None) != (expected_size is None):
        raise ValueError("expected digest and size must be supplied together")
    if expected_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("expected SHA-256 is invalid")
    if expected_size is not None and (expected_size < 1 or expected_size > maximum_size):
        raise ValueError("expected size is invalid")

    started = monotonic()
    deadline = started + timeout_seconds
    previous_handler = signal.signal(signal.SIGALRM, _deadline_expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    source_fd = -1
    destination_fd = -1
    destination_created = False
    try:
        _check_deadline(deadline)
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("release artifact must be a regular file")
        if before.st_size < 1 or before.st_size > maximum_size:
            raise ValueError("release artifact exceeds its snapshot size limit")
        _check_deadline(deadline)
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o400,
        )
        destination_created = True
        digest = hashlib.sha256()
        copied = 0
        while True:
            _check_deadline(deadline)
            chunk = os.read(source_fd, CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            copied += len(chunk)
            if copied > maximum_size:
                raise ValueError("release artifact exceeds its snapshot size limit")
            view = memoryview(chunk)
            while view:
                _check_deadline(deadline)
                written = os.write(destination_fd, view)
                if written < 1:
                    raise OSError("stable snapshot write made no progress")
                view = view[written:]
        _check_deadline(deadline)
        os.fsync(destination_fd)
        _check_deadline(deadline)
        after = os.fstat(source_fd)
        snapshot = os.fstat(destination_fd)
        actual_sha256 = digest.hexdigest()
        if _identity(before) != _identity(after) or copied != before.st_size:
            raise ValueError("release artifact changed while snapshotting")
        if not stat.S_ISREG(snapshot.st_mode) or snapshot.st_size != copied:
            raise ValueError("stable snapshot metadata mismatch")
        if expected_sha256 is not None and (
            actual_sha256 != expected_sha256 or copied != expected_size
        ):
            raise ValueError("release artifact does not match signed metadata")
        os.fchmod(destination_fd, 0o400)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            _check_deadline(deadline)
            os.fsync(directory_fd)
            _check_deadline(deadline)
        finally:
            os.close(directory_fd)
    except (OSError, ValueError):
        if destination_fd >= 0:
            os.close(destination_fd)
            destination_fd = -1
        if destination_created:
            destination.unlink(missing_ok=True)
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        elapsed = monotonic() - started
        if previous_timer[0] > 0:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.000001, previous_timer[0] - elapsed),
                previous_timer[1],
            )
        if source_fd >= 0:
            os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)
    return actual_sha256, copied


def verify_descriptor(
    descriptor: int,
    path: Path,
    *,
    expected_size: int,
) -> None:
    opened = os.fstat(descriptor)
    live = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or _identity(opened) != _identity(live)
        or opened.st_size != expected_size
        or os.lseek(descriptor, 0, os.SEEK_CUR) != 0
    ):
        raise ValueError("stable release descriptor no longer matches its private path")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--source", required=True, type=Path)
    snapshot.add_argument("--destination", required=True, type=Path)
    snapshot.add_argument("--maximum-size", required=True, type=int)
    snapshot.add_argument("--timeout-seconds", required=True, type=int)
    snapshot.add_argument("--expected-sha256")
    snapshot.add_argument("--expected-size", type=int)
    descriptor = subparsers.add_parser("verify-fd")
    descriptor.add_argument("--fd", required=True, type=int)
    descriptor.add_argument("--path", required=True, type=Path)
    descriptor.add_argument("--expected-size", required=True, type=int)
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            digest, size = create_snapshot(
                args.source,
                args.destination,
                maximum_size=args.maximum_size,
                timeout_seconds=args.timeout_seconds,
                expected_sha256=args.expected_sha256,
                expected_size=args.expected_size,
            )
            print(f"stable snapshot ready: sha256={digest} size={size}")
        else:
            verify_descriptor(args.fd, args.path, expected_size=args.expected_size)
            print("stable release descriptor verified")
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
