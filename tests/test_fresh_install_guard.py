from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "ci" / "assert-fresh-install-data-plane.sh"


def _write_fake_docker(tmp_path: Path) -> Path:
    fake = tmp_path / "docker"
    fake.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
scenario = os.environ.get("FAKE_DOCKER_SCENARIO", "empty")
log_path = os.environ.get("FAKE_DOCKER_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(args) + "\\n")

if args[0] == "version":
    if scenario == "version-failure":
        sys.exit(42)
    print("27.5.1")
elif args[:2] == ["volume", "ls"]:
    if scenario == "volume-list-failure":
        sys.exit(42)
    if scenario in {"benign", "volume-inspect-failure"}:
        print("cache_data")
    elif scenario == "target-volume-name":
        print("legacy_postgres_data")
    elif scenario in {"target-volume-label", "bad-volume-labels", "unknown-volume-labels"}:
        print("opaque_data")
elif args[:2] == ["volume", "inspect"]:
    if scenario == "volume-inspect-failure":
        sys.exit(42)
    template = args[args.index("--format") + 1]
    if "com.docker.compose.project" in template:
        value = {
            "benign": "cache-app",
            "target-volume-label": "legacy-edge",
            "bad-volume-labels": "drycontroller-edge",
        }.get(scenario, "<no value>")
    else:
        value = {
            "benign": "cache_data",
            "target-volume-label": "influxdb_data",
        }.get(scenario, "<no value>")
    print(value)
elif args[:2] == ["container", "ls"]:
    if scenario == "container-list-failure":
        sys.exit(42)
    if scenario in {
        "benign",
        "container-inspect-failure",
        "target-container-name",
        "target-container-service",
        "target-container-mount",
        "bad-container-labels",
    }:
        print("a" * 64)
elif args[:2] == ["container", "inspect"]:
    if scenario == "container-inspect-failure":
        sys.exit(42)
    template = args[args.index("--format") + 1]
    if template == "{{.Name}}":
        value = "/edge-postgres" if scenario == "target-container-name" else "/cache-worker"
    elif "com.docker.compose.project" in template:
        value = {
            "benign": "cache-app",
            "target-container-service": "legacy-edge",
            "target-container-mount": "cache-app",
            "bad-container-labels": "drycontroller-edge",
        }.get(scenario, "<no value>")
    elif "com.docker.compose.service" in template:
        value = {
            "benign": "worker",
            "target-container-service": "prometheus",
            "target-container-mount": "worker",
        }.get(scenario, "<no value>")
    elif ".Mounts" in template:
        value = "/var/lib/influxdb2" if scenario == "target-container-mount" else ""
    else:
        raise SystemExit(97)
    print(value)
else:
    raise SystemExit(98)
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _run_guard(
    tmp_path: Path,
    scenario: str,
    *,
    project: str = "drycontroller-edge",
    log_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    _write_fake_docker(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FAKE_DOCKER_SCENARIO": scenario,
    }
    if log_path is not None:
        env["FAKE_DOCKER_LOG"] = str(log_path)
    return subprocess.run(
        ["bash", str(GUARD), project],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_fresh_install_guard_accepts_empty_docker_state(tmp_path: Path):
    result = _run_guard(tmp_path, "empty")

    assert result.returncode == 0, result.stderr
    assert "fresh-install data-plane preflight passed" in result.stdout


def test_fresh_install_guard_accepts_classified_unrelated_resources(tmp_path: Path):
    result = _run_guard(tmp_path, "benign")

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("scenario", ["target-volume-name", "target-volume-label"])
def test_fresh_install_guard_rejects_existing_data_volume(tmp_path: Path, scenario: str):
    result = _run_guard(tmp_path, scenario)

    assert result.returncode != 0
    assert "existing target volume" in result.stderr


@pytest.mark.parametrize(
    ("scenario", "evidence"),
    [
        ("target-container-name", "edge-postgres"),
        ("target-container-service", "prometheus"),
        ("target-container-mount", "/var/lib/influxdb2"),
    ],
)
def test_fresh_install_guard_rejects_existing_data_container(
    tmp_path: Path,
    scenario: str,
    evidence: str,
):
    result = _run_guard(tmp_path, scenario)

    assert result.returncode != 0
    assert "existing target container" in result.stderr
    assert evidence in result.stderr


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("bad-volume-labels", "incomplete Compose ownership labels"),
        ("bad-container-labels", "incomplete Compose ownership labels"),
        ("unknown-volume-labels", "unknown Compose ownership labels"),
    ],
)
def test_fresh_install_guard_rejects_unknown_compose_ownership(
    tmp_path: Path,
    scenario: str,
    reason: str,
):
    result = _run_guard(tmp_path, scenario)

    assert result.returncode != 0
    assert reason in result.stderr


@pytest.mark.parametrize(
    "scenario",
    [
        "version-failure",
        "volume-list-failure",
        "volume-inspect-failure",
        "container-list-failure",
        "container-inspect-failure",
    ],
)
def test_fresh_install_guard_fails_closed_when_inspection_fails(tmp_path: Path, scenario: str):
    result = _run_guard(tmp_path, scenario)

    assert result.returncode != 0
    assert "fresh-install preflight refused" in result.stderr


def test_fresh_install_guard_only_calls_read_only_docker_commands(tmp_path: Path):
    log_path = tmp_path / "docker.log"
    result = _run_guard(tmp_path, "benign", log_path=log_path)

    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    allowed_prefixes = {
        ("version",),
        ("volume", "ls"),
        ("volume", "inspect"),
        ("container", "ls"),
        ("container", "inspect"),
    }
    assert calls
    assert all(any(tuple(call[: len(prefix)]) == prefix for prefix in allowed_prefixes) for call in calls)

    source = GUARD.read_text(encoding="utf-8")
    for mutation in ("docker rm", "docker stop", "docker create", "docker run", "docker compose", "prune"):
        assert mutation not in source


def test_fresh_install_guard_rejects_invalid_project_before_docker_access(tmp_path: Path):
    log_path = tmp_path / "docker.log"
    result = _run_guard(tmp_path, "empty", project="BAD/project", log_path=log_path)

    assert result.returncode != 0
    assert "invalid Compose project" in result.stderr
    assert not log_path.exists()
