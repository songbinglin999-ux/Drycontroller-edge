from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "ci" / "assert-fresh-install-data-plane.sh"
PROJECT = "drycontroller-edge"


def _write_fake_docker(tmp_path: Path) -> None:
    fake = tmp_path / "docker"
    fake.write_text(
        r'''#!/bin/sh
set -eu
scenario="${FAKE_DOCKER_SCENARIO:-empty}"
if [ -n "${FAKE_DOCKER_LOG:-}" ]; then
  printf '%s\n' "$*" >>"${FAKE_DOCKER_LOG}"
fi

case "$1:$2" in
  version:--format)
    case "$scenario" in
      version-failure) exit 42 ;;
      version-empty) exit 0 ;;
      version-malformed) printf '%s\n' 'not/a-version' ;;
      *) printf '%s\n' '27.5.1' ;;
    esac
    ;;
  volume:ls)
    case "$scenario" in
      volume-list-failure) exit 42 ;;
      volume-list-malformed) printf '%s\n' '--bad-volume' ;;
      target-volume) printf '%s_%s\n' "${FAKE_PROJECT:-drycontroller-edge}" "${FAKE_VOLUME_LOGICAL}" ;;
      other-project-volume) printf '%s\n' 'other_cache_data' ;;
      volume-name-spoof|unknown-volume-labels|volume-inspect-empty|volume-inspect-malformed|volume-inspect-failure)
        printf '%s\n' 'opaque_data'
        ;;
      volume-label-spoof) printf '%s\n' 'drycontroller-edge_postgres_data' ;;
    esac
    ;;
  volume:inspect)
    [ "$scenario" != volume-inspect-failure ] || exit 42
    template="$4"
    name="$5"
    [ "$scenario" != volume-inspect-empty ] || exit 0
    case "$template" in
      *'.Name'*)
        if [ "$scenario" = volume-inspect-malformed ]; then printf '%s\n' 'bad/name'; else printf '%s\n' "$name"; fi
        ;;
      *'com.docker.compose.project'*)
        case "$scenario" in
          other-project-volume) printf '%s\n' 'other' ;;
          unknown-volume-labels) printf '%s\n' '<no value>' ;;
          *) printf '%s\n' "${FAKE_PROJECT:-drycontroller-edge}" ;;
        esac
        ;;
      *'com.docker.compose.volume'*)
        case "$scenario" in
          other-project-volume) printf '%s\n' 'cache_data' ;;
          volume-name-spoof) printf '%s\n' 'postgres_data' ;;
          volume-label-spoof) printf '%s\n' 'mosquitto_data' ;;
          unknown-volume-labels) printf '%s\n' '<no value>' ;;
          *) printf '%s\n' "${FAKE_VOLUME_LOGICAL:-cache_data}" ;;
        esac
        ;;
      *) exit 97 ;;
    esac
    ;;
  container:ls)
    case "$scenario" in
      container-list-failure) exit 42 ;;
      container-list-malformed) printf '%s\n' 'not-an-id' ;;
      target-container|other-project-container|container-name-spoof|unknown-container-service|\
        mount-spoof|mount-missing|mount-extra|mount-malformed|container-inspect-empty|container-inspect-malformed|container-inspect-failure)
        printf '%064d\n' 0
        ;;
    esac
    ;;
  container:inspect)
    [ "$scenario" != container-inspect-failure ] || exit 42
    template="$4"
    [ "$scenario" != container-inspect-empty ] || exit 0
    service="${FAKE_CONTAINER_SERVICE:-postgres}"
    case "$service" in
      postgres) name=edge-postgres; mounts='volume|drycontroller-edge_postgres_data|/var/lib/postgresql/data' ;;
      influxdb) name=edge-influxdb; mounts='volume|drycontroller-edge_influxdb_data|/var/lib/influxdb2
volume|drycontroller-edge_influxdb_config|/etc/influxdb2' ;;
      mosquitto-init) name=edge-mosquitto-init; mounts='volume|drycontroller-edge_mosquitto_secrets|/mosquitto/secret' ;;
      mosquitto) name=edge-mosquitto; mounts='volume|drycontroller-edge_mosquitto_data|/mosquitto/data
volume|drycontroller-edge_mosquitto_logs|/mosquitto/log
volume|drycontroller-edge_mosquitto_secrets|/mosquitto/secret' ;;
      prometheus) name=edge-prometheus; mounts='volume|drycontroller-edge_prometheus_data|/prometheus' ;;
      alertmanager) name=edge-alertmanager; mounts='volume|drycontroller-edge_alertmanager_data|/alertmanager' ;;
      *) name=edge-api; mounts='' ;;
    esac
    case "$scenario" in
      other-project-container) name=other-worker ;;
      container-name-spoof) name=edge-postgres; service=influxdb ;;
      unknown-container-service) name=unknown-worker; service=unknown ;;
      mount-spoof) mounts='bind||/var/lib/postgresql/data' ;;
      mount-missing) mounts='' ;;
      mount-extra) mounts="$mounts
volume|drycontroller-edge_extra_data|/unexpected" ;;
      mount-malformed) mounts='volume|broken' ;;
    esac
    case "$template" in
      *'.Mounts'*) printf '%b\n' "$mounts" ;;
      *'.Id'*)
        if [ "$scenario" = container-inspect-malformed ]; then printf '%s\n' 'not-an-id'; else printf '%064d\n' 0; fi
        ;;
      *'.Name'*) printf '/%s\n' "$name" ;;
      *'com.docker.compose.project'*)
        if [ "$scenario" = other-project-container ]; then printf '%s\n' other; else printf '%s\n' drycontroller-edge; fi
        ;;
      *'com.docker.compose.service'*) printf '%s\n' "$service" ;;
      *) exit 96 ;;
    esac
    ;;
  *) exit 98 ;;
esac
''',
        encoding="utf-8",
    )
    fake.chmod(0o755)


def _run_guard(
    tmp_path: Path,
    scenario: str,
    *,
    project: str = PROJECT,
    extra_env: dict[str, str] | None = None,
    log_path: Path | None = None,
    include_lock: bool = True,
) -> subprocess.CompletedProcess[str]:
    _write_fake_docker(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FAKE_DOCKER_SCENARIO": scenario,
        **(extra_env or {}),
    }
    if log_path is not None:
        env["FAKE_DOCKER_LOG"] = str(log_path)
    descriptor = os.open(tmp_path / "release.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        args = ["bash", str(GUARD), "--project", project]
        pass_fds: tuple[int, ...] = ()
        if include_lock:
            args.extend(["--lock-fd", str(descriptor)])
            pass_fds = (descriptor,)
        return subprocess.run(
            args,
            cwd=ROOT,
            env=env,
            pass_fds=pass_fds,
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        os.close(descriptor)


def test_guard_accepts_only_an_empty_daemon_under_the_transaction_lock(tmp_path: Path):
    result = _run_guard(tmp_path, "empty")

    assert result.returncode == 0, result.stderr
    assert "fresh-install data-plane preflight passed" in result.stdout


def test_guard_contract_covers_every_compose_volume_container_and_persistent_mount():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    source = GUARD.read_text(encoding="utf-8")

    assert set(compose["volumes"]) == {
        "postgres_data",
        "influxdb_data",
        "influxdb_config",
        "mosquitto_data",
        "mosquitto_logs",
        "mosquitto_secrets",
        "prometheus_data",
        "alertmanager_data",
    }
    for volume in compose["volumes"]:
        assert volume in source
    for service_name, service in compose["services"].items():
        assert service_name in source
        assert service["container_name"] in source
        for mount in service.get("volumes", []):
            source_name, destination, *_ = mount.split(":")
            if source_name in compose["volumes"]:
                assert destination in source


@pytest.mark.parametrize(
    "logical",
    [
        "postgres_data",
        "influxdb_data",
        "influxdb_config",
        "mosquitto_data",
        "mosquitto_logs",
        "mosquitto_secrets",
        "prometheus_data",
        "alertmanager_data",
    ],
)
def test_guard_rejects_every_existing_persistent_volume(tmp_path: Path, logical: str):
    result = _run_guard(
        tmp_path,
        "target-volume",
        extra_env={"FAKE_VOLUME_LOGICAL": logical},
    )

    assert result.returncode != 0
    assert "existing target volume" in result.stderr


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("other-project-volume", "project mismatch"),
        ("volume-name-spoof", "name/label conflict"),
        ("volume-label-spoof", "name/label conflict"),
        ("unknown-volume-labels", "ownership labels"),
    ],
)
def test_guard_rejects_volume_ownership_conflicts(
    tmp_path: Path,
    scenario: str,
    reason: str,
):
    result = _run_guard(tmp_path, scenario)

    assert result.returncode != 0
    assert reason in result.stderr


@pytest.mark.parametrize(
    "service",
    ["postgres", "influxdb", "mosquitto-init", "mosquitto", "prometheus", "alertmanager"],
)
def test_guard_rejects_every_existing_persistent_service_container(
    tmp_path: Path,
    service: str,
):
    result = _run_guard(
        tmp_path,
        "target-container",
        extra_env={"FAKE_CONTAINER_SERVICE": service},
    )

    assert result.returncode != 0
    assert "existing target container" in result.stderr


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("other-project-container", "project mismatch"),
        ("container-name-spoof", "name/service conflict"),
        ("unknown-container-service", "unknown Compose service"),
        ("mount-spoof", "persistent mount conflict"),
        ("mount-missing", "persistent mount missing"),
        ("mount-extra", "unknown persistent mount"),
    ],
)
def test_guard_rejects_container_and_mount_contract_conflicts(
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
        "version-empty",
        "version-malformed",
        "volume-list-failure",
        "volume-list-malformed",
        "volume-inspect-failure",
        "volume-inspect-empty",
        "volume-inspect-malformed",
        "container-list-failure",
        "container-list-malformed",
        "container-inspect-failure",
        "container-inspect-empty",
        "container-inspect-malformed",
        "mount-malformed",
    ],
)
def test_guard_fails_closed_on_command_failure_or_malformed_success(
    tmp_path: Path,
    scenario: str,
):
    result = _run_guard(tmp_path, scenario)

    assert result.returncode != 0
    assert "fresh-install preflight refused" in result.stderr


def test_guard_requires_the_formal_project_and_inherited_lock(tmp_path: Path):
    wrong_project = _run_guard(tmp_path, "empty", project="other")
    no_lock = _run_guard(tmp_path, "empty", include_lock=False)

    assert wrong_project.returncode != 0
    assert "project mismatch" in wrong_project.stderr
    assert no_lock.returncode != 0
    assert "lock" in no_lock.stderr


def test_guard_only_calls_read_only_docker_commands(tmp_path: Path):
    log_path = tmp_path / "docker.log"
    result = _run_guard(tmp_path, "target-container", log_path=log_path)

    assert result.returncode != 0
    calls = log_path.read_text(encoding="utf-8").splitlines()
    allowed = (
        "version --format ",
        "volume ls --quiet",
        "volume inspect --format ",
        "container ls --all --quiet --no-trunc",
        "container inspect --format ",
    )
    assert calls
    assert all(call.startswith(allowed) for call in calls)

    source = GUARD.read_text(encoding="utf-8")
    for mutation in (
        "docker rm",
        "docker stop",
        "docker create",
        "docker run",
        "docker compose",
        "docker volume rm",
        "prune",
    ):
        assert mutation not in source
