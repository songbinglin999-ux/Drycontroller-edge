from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "scripts/ci/verify-edge-app-migration-scope.py"
SPEC = importlib.util.spec_from_file_location("verify_edge_app_migration_scope", GATE_PATH)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "services/edge-api/alembic/versions", "services/edge-api/app")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_gate_allows_application_change_without_alembic_revision_change(tmp_path: Path):
    repo = tmp_path / "repo"
    versions = repo / "services/edge-api/alembic/versions"
    app = repo / "services/edge-api/app"
    versions.mkdir(parents=True)
    app.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "edge-app-test@example.invalid")
    _git(repo, "config", "user.name", "Edge App Test")
    (versions / "baseline.py").write_text("revision = 'baseline'\n")
    (app / "main.py").write_text("VALUE = 1\n")
    baseline = _commit(repo, "baseline")
    (app / "main.py").write_text("VALUE = 2\n")
    source = _commit(repo, "app only")

    gate.verify_migration_scope(repo=repo, approved_baseline=baseline, source_commit=source)


def test_gate_rejects_alembic_revision_change_in_approved_source_range(tmp_path: Path):
    repo = tmp_path / "repo"
    versions = repo / "services/edge-api/alembic/versions"
    app = repo / "services/edge-api/app"
    versions.mkdir(parents=True)
    app.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "edge-app-test@example.invalid")
    _git(repo, "config", "user.name", "Edge App Test")
    (versions / "baseline.py").write_text("revision = 'baseline'\n")
    (app / "main.py").write_text("VALUE = 1\n")
    baseline = _commit(repo, "baseline")
    (versions / "candidate.py").write_text("revision = 'candidate'\n")
    source = _commit(repo, "migration")

    with pytest.raises(gate.MigrationScopeError, match="Alembic revision change"):
        gate.verify_migration_scope(repo=repo, approved_baseline=baseline, source_commit=source)
