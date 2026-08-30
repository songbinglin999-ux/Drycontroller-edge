#!/usr/bin/env python3
"""Fail closed when an edge-app-v1 source range changes Alembic revisions."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess


COMMIT = re.compile(r"^[0-9a-f]{40}$")
REVISION_PATH = "services/edge-api/alembic/versions"


class MigrationScopeError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MigrationScopeError("Git source range inspection unavailable") from exc
    if check and completed.returncode != 0:
        raise MigrationScopeError("Git source range inspection failed")
    return completed


def verify_migration_scope(*, repo: Path, approved_baseline: str, source_commit: str) -> None:
    if COMMIT.fullmatch(approved_baseline) is None or COMMIT.fullmatch(source_commit) is None:
        raise MigrationScopeError("approved baseline and source must be lowercase 40-hex commits")
    repo = repo.resolve(strict=True)
    for commit in (approved_baseline, source_commit):
        _git(repo, "cat-file", "-e", f"{commit}^{{commit}}")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if head != source_commit:
        raise MigrationScopeError("release source commit is not the checked-out source")
    ancestor = _git(
        repo,
        "merge-base",
        "--is-ancestor",
        approved_baseline,
        source_commit,
        check=False,
    )
    if ancestor.returncode != 0:
        raise MigrationScopeError("approved database baseline is not an ancestor of release source")
    changed = _git(
        repo,
        "diff",
        "--name-only",
        f"{approved_baseline}..{source_commit}",
        "--",
        REVISION_PATH,
    ).stdout.splitlines()
    if changed:
        raise MigrationScopeError(
            "edge-app-v1 OTA forbids Alembic revision changes: " + ", ".join(sorted(changed))
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--approved-baseline", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        verify_migration_scope(
            repo=args.repo,
            approved_baseline=args.approved_baseline,
            source_commit=args.source,
        )
    except MigrationScopeError as exc:
        parser.exit(1, f"edge-app migration gate failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
