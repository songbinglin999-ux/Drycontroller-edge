from __future__ import annotations

import hashlib
import os
import runpy
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "stable-release-artifact.py"


def _module() -> dict[str, object]:
    return runpy.run_path(str(HELPER))


def test_signed_snapshot_is_independent_of_later_source_path_swap(tmp_path: Path):
    expected = b"signed public installer bytes"
    source = tmp_path / "installer.iso"
    snapshot = tmp_path / "stable.iso"
    source.write_bytes(expected)
    helper = _module()

    digest, size = helper["create_snapshot"](
        source,
        snapshot,
        maximum_size=1024,
        timeout_seconds=30,
        expected_sha256=hashlib.sha256(expected).hexdigest(),
        expected_size=len(expected),
    )
    replacement = tmp_path / "replacement.iso"
    replacement.write_bytes(b"malicious replacement")
    replacement.replace(source)

    assert digest == hashlib.sha256(expected).hexdigest()
    assert size == len(expected)
    assert snapshot.read_bytes() == expected
    assert source.read_bytes() != expected


def test_open_snapshot_descriptor_rejects_private_path_replacement(tmp_path: Path):
    expected = b"signed bytes"
    source = tmp_path / "source.iso"
    snapshot = tmp_path / "stable.iso"
    source.write_bytes(expected)
    helper = _module()
    helper["create_snapshot"](
        source,
        snapshot,
        maximum_size=1024,
        timeout_seconds=30,
    )
    descriptor = os.open(snapshot, os.O_RDONLY)
    try:
        replacement = tmp_path / "replacement.iso"
        replacement.write_bytes(b"replacement")
        replacement.replace(snapshot)

        with pytest.raises(ValueError, match="descriptor no longer matches"):
            helper["verify_descriptor"](
                descriptor,
                snapshot,
                expected_size=len(expected),
            )
        assert os.read(descriptor, len(expected)) == expected
    finally:
        os.close(descriptor)


def test_snapshot_rejects_symlink_source(tmp_path: Path):
    target = tmp_path / "target.iso"
    source = tmp_path / "source.iso"
    target.write_bytes(b"signed bytes")
    source.symlink_to(target)

    with pytest.raises(OSError):
        _module()["create_snapshot"](
            source,
            tmp_path / "stable.iso",
            maximum_size=1024,
            timeout_seconds=30,
        )


def test_snapshot_refuses_to_replace_existing_destination(tmp_path: Path):
    source = tmp_path / "source.iso"
    destination = tmp_path / "stable.iso"
    source.write_bytes(b"signed bytes")
    destination.write_bytes(b"must remain")

    with pytest.raises(FileExistsError):
        _module()["create_snapshot"](
            source,
            destination,
            maximum_size=1024,
            timeout_seconds=30,
        )

    assert destination.read_bytes() == b"must remain"


def test_snapshot_rejects_oversized_sparse_source_before_copy(tmp_path: Path):
    source = tmp_path / "oversized.manifest"
    destination = tmp_path / "stable.manifest"
    with source.open("wb") as stream:
        stream.truncate(1025)

    with pytest.raises(ValueError, match="snapshot size limit"):
        _module()["create_snapshot"](
            source,
            destination,
            maximum_size=1024,
            timeout_seconds=30,
        )

    assert not destination.exists()


def test_snapshot_enforces_deadline_before_copy(tmp_path: Path, monkeypatch):
    source = tmp_path / "manifest.json"
    destination = tmp_path / "stable.manifest"
    source.write_bytes(b"{}")
    helper = _module()
    clock = iter((0.0, 2.0, 2.0))
    monkeypatch.setitem(helper["create_snapshot"].__globals__, "monotonic", lambda: next(clock))

    with pytest.raises(TimeoutError, match="deadline exceeded"):
        helper["create_snapshot"](
            source,
            destination,
            maximum_size=1024,
            timeout_seconds=1,
        )

    assert not destination.exists()
