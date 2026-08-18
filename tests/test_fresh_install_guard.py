from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "ci" / "assert-fresh-install-data-plane.sh"
REAL_DOCKER_E2E = ROOT / "scripts" / "ci" / "test-fresh-install-data-plane-docker.sh"
PROJECT = "drycontroller-edge"


def _write_fake_docker(tmp_path: Path) -> Path:
    fake = tmp_path / "docker"
    fake.write_text(
        r'''#!/bin/sh
set -eu
scenario="${FAKE_DOCKER_SCENARIO:-empty}"
if [ -n "${FAKE_DOCKER_LOG:-}" ]; then
  printf '%s\n' "$*" >>"${FAKE_DOCKER_LOG}"
fi

case "$1:$2" in
  info:--format)
    printf '%s\n' 'daemon-id' 'daemon-name' '/var/lib/docker-test'
    ;;
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
    return fake


def _write_guard_under_test(tmp_path: Path, docker: Path, lock: Path) -> Path:
    source = GUARD.read_text(encoding="utf-8")
    docker_constant = "readonly DOCKER='/usr/bin/docker'"
    lock_constant = "readonly RELEASE_LOCK='/var/lib/drycontroller-ota/release.lock'"
    config_constant = "readonly DOCKER_CONFIG_DIR='/var/lib/drycontroller-ota/guard-docker-config'"
    tools_constant = "readonly TOOL_IDENTITY='/etc/drycontroller/runtime-tools.json'"
    daemon_constant = "readonly DAEMON_IDENTITY='/etc/drycontroller/docker-daemon.json'"
    socket_constant = "readonly DOCKER_SOCKET='/run/docker.sock'"
    trust_constant = "readonly TRUST_ROOT='/'"
    socket_trust_constant = "readonly SOCKET_TRUST_ROOT='/'"
    socket_kind_constant = "readonly DOCKER_SOCKET_KIND='socket'"
    assert source.count(docker_constant) == 1
    assert source.count(lock_constant) == 1
    assert source.count(config_constant) == 1
    assert source.count(tools_constant) == 1
    assert source.count(daemon_constant) == 1
    assert source.count(socket_constant) == 1
    assert source.count(trust_constant) == 1
    assert source.count(socket_trust_constant) == 1
    assert source.count(socket_kind_constant) == 1
    config_directory = tmp_path / "guard-docker-config"
    if config_directory.exists():
        config_directory.chmod(0o700)
    else:
        config_directory.mkdir(mode=0o700)
    config = config_directory / "config.json"
    if config.exists():
        config.chmod(0o600)
    config.write_text('{"auths":{}}', encoding="utf-8")
    config.chmod(0o400)
    config_directory.chmod(0o500)
    tool_identity = tmp_path / "runtime-tools.json"
    if tool_identity.exists():
        tool_identity.chmod(0o600)
    tool_identity.write_text(
        json.dumps(
            {
                "schema": "drycontroller.runtime-tools.v1",
                "executables": {
                    str(docker): {
                        "sha256": "sha256:" + hashlib.sha256(docker.read_bytes()).hexdigest()
                    },
                    "/usr/bin/bash": {"sha256": "sha256:" + "0" * 64},
                    "/usr/libexec/docker/cli-plugins/docker-compose": {
                        "sha256": "sha256:" + "1" * 64
                    },
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    tool_identity.chmod(0o400)
    daemon_identity = tmp_path / "docker-daemon.json"
    if daemon_identity.exists():
        daemon_identity.chmod(0o600)
    daemon_identity.write_text(
        json.dumps(
            {
                "schema": "drycontroller.docker-daemon.v1",
                "id": "daemon-id",
                "name": "daemon-name",
                "root_dir": "/var/lib/docker-test",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    daemon_identity.chmod(0o400)
    docker_socket = tmp_path / "docker.sock"
    docker_socket.write_bytes(b"")
    docker_socket.chmod(0o600)
    tmp_path.chmod(0o700)
    source = source.replace(docker_constant, f"readonly DOCKER='{docker}'")
    source = source.replace(lock_constant, f"readonly RELEASE_LOCK='{lock}'")
    source = source.replace(config_constant, f"readonly DOCKER_CONFIG_DIR='{config_directory}'")
    source = source.replace(tools_constant, f"readonly TOOL_IDENTITY='{tool_identity}'")
    source = source.replace(daemon_constant, f"readonly DAEMON_IDENTITY='{daemon_identity}'")
    source = source.replace(socket_constant, f"readonly DOCKER_SOCKET='{docker_socket}'")
    source = source.replace(trust_constant, f"readonly TRUST_ROOT='{tmp_path}'")
    source = source.replace(
        socket_trust_constant,
        f"readonly SOCKET_TRUST_ROOT='{tmp_path}'",
    )
    source = source.replace(
        socket_kind_constant,
        "readonly DOCKER_SOCKET_KIND='regular-test'",
    )
    source = source.replace("stat -c '%d:%i'", "stat -f '%d:%i'")
    source = source.replace(
        'readonly DOCKER_EXEC="/proc/self/fd/${DOCKER_FD}"',
        f"readonly DOCKER_EXEC='{docker}'",
    )
    guard = tmp_path / "assert-fresh-install-data-plane-under-test"
    guard.write_text(source, encoding="utf-8")
    guard.chmod(0o755)
    return guard


def _run_guard(
    tmp_path: Path,
    scenario: str,
    *,
    project: str = PROJECT,
    extra_env: dict[str, str] | None = None,
    log_path: Path | None = None,
    include_lock: bool = True,
    lock_held: bool = True,
) -> subprocess.CompletedProcess[str]:
    docker = _write_fake_docker(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FAKE_DOCKER_SCENARIO": scenario,
        **(extra_env or {}),
    }
    if log_path is not None:
        env["FAKE_DOCKER_LOG"] = str(log_path)
    lock = tmp_path / "release.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        if lock_held:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        guard = _write_guard_under_test(tmp_path, docker, lock)
        args = ["/bin/bash", str(guard), "--project", project]
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


@pytest.mark.skipif(
    os.environ.get("DRYCONTROLLER_FRESH_GUARD_ISOLATED_DAEMON") != "1",
    reason="requires an explicitly marked isolated disposable Docker daemon",
)
def test_guard_against_an_isolated_real_docker_daemon():
    result = subprocess.run(
        ["/usr/bin/bash", str(REAL_DOCKER_E2E)],
        cwd=ROOT,
        env=os.environ,
        text=True,
        capture_output=True,
        check=False,
        timeout=5 * 60,
    )

    assert result.returncode == 0, result.stderr
    assert (
        "self-dockerd=1 installer=exact initializer=signed-pair refresh=live-edge-api "
        "private-pull=authenticated-tls ota-apply=public-entrypoint "
        "local-auth=missing,malformed path-context=refused tool-content=refused "
        "ancestor=refused daemon=refused socket=refused"
        in result.stdout
    )


def test_real_docker_e2e_uses_only_formal_public_entrypoints():
    source = REAL_DOCKER_E2E.read_text(encoding="utf-8")

    assert "infra/bootstrap/install-edge-host.sh" in source
    assert "scripts/prepare-host-release.py\" stage" in source
    assert "/usr/libexec/drycontroller/initialize-initial-release" not in source
    assert "/infra/ota-helper/ota-apply" in source
    assert "/api/v1/ops/registry-credentials/refresh" in source
    assert "_assert_fresh_install" not in source
    assert "host_release_engine.HostReleaseEngine" not in source
    assert "cat >\"${compose_file}\"" not in source


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


def test_guard_rejects_a_matching_but_unlocked_descriptor(tmp_path: Path):
    result = _run_guard(tmp_path, "empty", lock_held=False)

    assert result.returncode != 0
    assert "lock" in result.stderr


def test_guard_pins_production_docker_and_release_lock_paths():
    source = GUARD.read_text(encoding="utf-8")

    assert source.startswith("#!/usr/bin/bash\n")
    assert "readonly DOCKER='/usr/bin/docker'" in source
    assert "readonly RELEASE_LOCK='/var/lib/drycontroller-ota/release.lock'" in source
    assert "DOCKER_HOST='unix:///run/docker.sock'" in source
    assert "command -v docker" not in source


def test_guard_ignores_a_path_precedence_docker_that_fakes_an_empty_daemon(
    tmp_path: Path,
):
    trusted_dir = tmp_path / "usr" / "bin"
    trusted_dir.mkdir(parents=True)
    trusted = _write_fake_docker(trusted_dir)
    malicious_dir = tmp_path / "usr" / "local" / "bin"
    malicious_dir.mkdir(parents=True)
    malicious = malicious_dir / "docker"
    malicious_log = tmp_path / "malicious-docker.log"
    malicious.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >>'{malicious_log}'\n"
        "case \"$1\" in version) echo 99.0.0;; esac\n",
        encoding="utf-8",
    )
    malicious.chmod(0o755)
    lock = tmp_path / "release.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        guard = _write_guard_under_test(tmp_path, trusted, lock)
        result = subprocess.run(
            [
                "/bin/bash",
                str(guard),
                "--project",
                PROJECT,
                "--lock-fd",
                str(descriptor),
            ],
            env={
                **os.environ,
                "PATH": f"{malicious_dir}:{os.environ['PATH']}",
                "FAKE_DOCKER_SCENARIO": "target-volume",
                "FAKE_VOLUME_LOGICAL": "postgres_data",
            },
            pass_fds=(descriptor,),
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        os.close(descriptor)

    assert result.returncode != 0
    assert "existing target volume" in result.stderr
    assert not malicious_log.exists()


def test_guard_ignores_path_bash_wrapper_and_host_context_config_injection(tmp_path: Path):
    malicious_dir = tmp_path / "usr" / "local" / "bin"
    malicious_dir.mkdir(parents=True)
    malicious_log = tmp_path / "malicious-bash.log"
    wrapper = malicious_dir / "bash"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"printf invoked >'{malicious_log}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = _run_guard(
        tmp_path,
        "target-volume",
        extra_env={
            "PATH": f"{malicious_dir}:/usr/bin:/bin",
            "DOCKER_HOST": "tcp://attacker.invalid:2375",
            "DOCKER_CONTEXT": "attacker",
            "DOCKER_CONFIG": str(tmp_path / "attacker-config"),
            "DOCKER_CLI_PLUGIN_EXTRA_DIRS": str(tmp_path / "plugins"),
            "FAKE_VOLUME_LOGICAL": "postgres_data",
        },
    )

    assert result.returncode != 0
    assert "existing target volume" in result.stderr
    assert not malicious_log.exists()


def test_guard_only_calls_read_only_docker_commands(tmp_path: Path):
    log_path = tmp_path / "docker.log"
    result = _run_guard(tmp_path, "target-container", log_path=log_path)

    assert result.returncode != 0
    calls = log_path.read_text(encoding="utf-8").splitlines()
    allowed = (
        "info --format ",
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
