#!/usr/bin/env python3
"""Fail closed when a source bundle contains macOS metadata or is unreadable."""

from __future__ import annotations

import gzip
import runpy
import sys
import tarfile
import time
from pathlib import Path, PurePosixPath


def validate_source_bundle(path: Path) -> None:
    if not path.is_file():
        raise ValueError("source bundle is missing")
    with gzip.open(path, "rb") as compressed:
        while compressed.read(1024 * 1024):
            pass
    with tarfile.open(path, "r:gz", errorlevel=2) as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if any(part == ".DS_Store" or part == "__MACOSX" or part.startswith("._") for part in parts):
                raise ValueError("source bundle contains forbidden macOS metadata")
    scanner = runpy.run_path(str(Path(__file__).with_name("public-material-scan.py")))
    result = scanner["scan_source"](path, deadline=time.monotonic() + 1800)
    if result.get("passed") is not True:
        raise ValueError("source bundle failed public-material scan")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate-source-bundle.py BUNDLE", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    try:
        validate_source_bundle(path)
    except (EOFError, OSError, tarfile.TarError, ValueError) as exc:
        path.unlink(missing_ok=True)
        print(f"source bundle validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
