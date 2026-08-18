#!/usr/bin/bash

set -euo pipefail

readonly DOCKER='/usr/bin/docker'
readonly DOCKERD='/usr/bin/dockerd'
readonly COMPOSE='/usr/libexec/docker/cli-plugins/docker-compose'
readonly BASH='/usr/bin/bash'
readonly PYTHON='/usr/bin/python3'
readonly SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly FORMAL_ROOT='/usr/libexec/drycontroller'
readonly FORMAL_GUARD="${FORMAL_ROOT}/assert-fresh-install-data-plane"
readonly FORMAL_ENGINE="${FORMAL_ROOT}/host_release_engine.py"
readonly STATE_ROOT='/var/lib/drycontroller-ota'
readonly LOCK="${STATE_ROOT}/release.lock"
readonly GUARD_CONFIG="${STATE_ROOT}/guard-docker-config"
readonly AUTH_SOURCE="${STATE_ROOT}/registry-auth.json"
readonly ISOLATION_MARKER='/etc/drycontroller-rb8a2-isolated-daemon'
readonly PROJECT='drycontroller-edge'

fail() {
  printf 'fresh-install real Docker E2E failed: %s\n' "$1" >&2
  exit 1
}

[ "${EUID}" -eq 0 ] || fail 'must run as root inside a disposable Linux environment'
[ "$(uname -s)" = 'Linux' ] || fail 'Linux is required'
[ "${DRYCONTROLLER_FRESH_GUARD_ISOLATED_DAEMON:-}" = '1' ] \
  || fail 'isolated daemon acknowledgement is required'
[ -f "${ISOLATION_MARKER}" ] && [ ! -L "${ISOLATION_MARKER}" ] \
  || fail 'root-only isolation marker is absent'
[ "$(stat -c '%u:%a' "${ISOLATION_MARKER}")" = '0:400' ] \
  || fail 'isolation marker ownership or mode is invalid'
[ -x "${DOCKER}" ] && [ ! -L "${DOCKER}" ] || fail 'fixed Docker executable is absent or linked'
[ -x "${DOCKERD}" ] && [ ! -L "${DOCKERD}" ] || fail 'fixed dockerd executable is absent or linked'
[ -x "${COMPOSE}" ] && [ ! -L "${COMPOSE}" ] || fail 'fixed Docker Compose executable is absent or linked'
[ -x "${BASH}" ] && [ ! -L "${BASH}" ] || fail 'fixed Bash executable is absent or linked'
[ -x "${PYTHON}" ] || fail 'fixed Python executable is absent'
[ ! -e /var/run/docker.sock ] || fail 'an existing Docker socket is present; refusing to connect or replace it'
[ ! -e "${FORMAL_ROOT}" ] || fail 'formal runtime path already exists in the disposable environment'
[ ! -e "${STATE_ROOT}" ] || fail 'formal OTA state path already exists in the disposable environment'
[ ! -e /usr/local/bin/bash ] || fail 'PATH Bash override already exists in the disposable environment'

tmp_dir="$(mktemp -d /tmp/drycontroller-rb8a2-e2e.XXXXXX)"
readonly tmp_dir
readonly daemon_data="${tmp_dir}/daemon-data"
readonly daemon_exec="${tmp_dir}/daemon-exec"
readonly daemon_pid="${tmp_dir}/dockerd.pid"
readonly daemon_log="${tmp_dir}/dockerd.log"
readonly compose_file="${tmp_dir}/docker-compose.yml"
readonly malicious_config="${tmp_dir}/malicious-docker-config"
readonly malicious_plugins="${tmp_dir}/malicious-plugins"
readonly malicious_bash_marker="${tmp_dir}/malicious-bash-invoked"
readonly malicious_plugin_marker="${tmp_dir}/malicious-compose-invoked"
readonly protected_image='drycontroller.local/rb8a2-non-task:protected'
readonly conflict_image='drycontroller.local/rb8a2-fresh-guard-e2e:conflict'
readonly task_image="drycontroller.local/rb8a2-fresh-guard-e2e:task-${$}"

dockerd_started=0
formal_installed=0
path_wrapper_created=0
compose_started=0
owned_image_id=''
socket_device=''
socket_inode=''

docker_exact() {
  env -i HOME=/var/empty PATH=/usr/bin:/bin \
    DOCKER_HOST=unix:///var/run/docker.sock DOCKER_CONFIG="${GUARD_CONFIG}" \
    "${DOCKER}" "$@"
}

compose_exact() {
  env -i HOME=/var/empty PATH=/usr/bin:/bin \
    DOCKER_HOST=unix:///var/run/docker.sock DOCKER_CONFIG="${GUARD_CONFIG}" \
    COMPOSE_PROJECT_NAME="${PROJECT}" "${COMPOSE}" "$@"
}

remove_owned_image() {
  local current_id
  [ -n "${owned_image_id}" ] || return 0
  current_id="$(docker_exact image inspect --format '{{.Id}}' "${task_image}" 2>/dev/null || true)"
  if [ -n "${current_id}" ] && [ "${current_id}" = "${owned_image_id}" ]; then
    docker_exact image rm "${task_image}" >/dev/null
  elif [ -n "${current_id}" ]; then
    fail 'task image tag changed ownership; refusing image deletion'
  fi
}

cleanup() {
  local daemon_process
  local current_socket
  set +e
  if [ "${compose_started}" -eq 1 ]; then
    compose_exact --project-name "${PROJECT}" --file "${compose_file}" down --volumes --remove-orphans \
      >/dev/null 2>&1 || true
  fi
  remove_owned_image >/dev/null 2>&1 || true
  if [ "${path_wrapper_created}" -eq 1 ]; then
    rm -f /usr/local/bin/bash
  fi
  if [ "${dockerd_started}" -eq 1 ] && [ -f "${daemon_pid}" ]; then
    daemon_process="$(cat "${daemon_pid}")"
    kill -TERM "${daemon_process}" >/dev/null 2>&1 || true
    for _ in $(seq 1 100); do
      kill -0 "${daemon_process}" >/dev/null 2>&1 || break
      sleep 0.05
    done
    kill -KILL "${daemon_process}" >/dev/null 2>&1 || true
  fi
  if [ -S /var/run/docker.sock ]; then
    current_socket="$(stat -c '%d:%i' /var/run/docker.sock 2>/dev/null || true)"
    if [ "${current_socket}" = "${socket_device}:${socket_inode}" ]; then
      rm -f /var/run/docker.sock
    fi
  fi
  if [ "${formal_installed}" -eq 1 ]; then
    rm -f "${FORMAL_GUARD}" "${FORMAL_ENGINE}"
    if [ -d "${FORMAL_ROOT}/__pycache__" ]; then
      find "${FORMAL_ROOT}/__pycache__" -maxdepth 1 -type f \
        -name 'host_release_engine.*.pyc' -delete
      rmdir "${FORMAL_ROOT}/__pycache__" >/dev/null 2>&1 || true
    fi
    rmdir "${FORMAL_ROOT}" >/dev/null 2>&1 || true
    rm -f "${GUARD_CONFIG}/config.json" "${AUTH_SOURCE}" "${LOCK}"
    rmdir "${GUARD_CONFIG}" "${STATE_ROOT}" >/dev/null 2>&1 || true
  fi
  rm -rf "${tmp_dir}"
}
trap cleanup EXIT

install -d -o root -g root -m 0700 "${daemon_data}" "${daemon_exec}"
"${DOCKERD}" \
  --host=unix:///var/run/docker.sock \
  --data-root="${daemon_data}" \
  --exec-root="${daemon_exec}" \
  --pidfile="${daemon_pid}" \
  --bridge=none \
  --iptables=false \
  --ip6tables=false \
  --ip-forward=false \
  --ip-masq=false \
  >"${daemon_log}" 2>&1 &
dockerd_started=1
for _ in $(seq 1 200); do
  [ -S /var/run/docker.sock ] && [ -f "${daemon_pid}" ] && break
  kill -0 "$!" >/dev/null 2>&1 || {
    tail -100 "${daemon_log}" >&2
    fail 'exact disposable dockerd exited during startup'
  }
  sleep 0.05
done
[ -S /var/run/docker.sock ] && [ -f "${daemon_pid}" ] || fail 'exact disposable dockerd did not start'
socket_device="$(stat -c '%d' /var/run/docker.sock)"
socket_inode="$(stat -c '%i' /var/run/docker.sock)"

install -d -o root -g root -m 0755 "${FORMAL_ROOT}"
install -d -o root -g root -m 0700 "${STATE_ROOT}"
install -d -o root -g root -m 0500 "${GUARD_CONFIG}"
install -o root -g root -m 0755 \
  "${SOURCE_ROOT}/scripts/ci/assert-fresh-install-data-plane.sh" "${FORMAL_GUARD}"
install -o root -g root -m 0644 \
  "${SOURCE_ROOT}/infra/host-release-runner/host_release_engine.py" "${FORMAL_ENGINE}"
printf '%s' '{"auths":{}}' >"${AUTH_SOURCE}"
chown root:root "${AUTH_SOURCE}"
chmod 0400 "${AUTH_SOURCE}"
printf '%s' '{"auths":{}}' >"${GUARD_CONFIG}/config.json"
chown root:root "${GUARD_CONFIG}/config.json"
chmod 0400 "${GUARD_CONFIG}/config.json"
chmod 0500 "${GUARD_CONFIG}"
install -o root -g root -m 0600 /dev/null "${LOCK}"
formal_installed=1

for _ in $(seq 1 200); do
  if docker_exact version --format '{{.Server.Version}}' >/dev/null 2>&1; then
    break
  fi
  sleep 0.05
done
[ "$(docker_exact info --format '{{.DockerRootDir}}')" = "${daemon_data}" ] \
  || fail 'Docker CLI did not bind to the exact disposable daemon data root'
[ -z "$(docker_exact container ls --all --quiet --no-trunc)" ] || fail 'exact daemon is not container-empty'
[ -z "$(docker_exact volume ls --quiet)" ] || fail 'exact daemon is not volume-empty'

expect_guard_failure_fd() {
  local descriptor="$1"
  local context="$2"
  local output
  if output="$("${BASH}" "${FORMAL_GUARD}" --project "${PROJECT}" --lock-fd "${descriptor}" 2>&1)"; then
    fail "guard accepted ${context}"
  fi
  case "${output}" in
    *'fresh-install preflight refused:'*) ;;
    *) fail "guard returned an unexpected failure for ${context}" ;;
  esac
}

wrong_lock="${tmp_dir}/wrong-release.lock"
install -o root -g root -m 0600 /dev/null "${wrong_lock}"
exec 7<>"${wrong_lock}"
flock -x 7
expect_guard_failure_fd 7 'a locked wrong inode'
exec 7>&-

exec 7<>"${LOCK}"
flock -s 7
expect_guard_failure_fd 7 'a shared release lock'
exec 7>&-

exec 7<>"${LOCK}"
flock -x 7
exec 7>&-
exec 7<>"${LOCK}"
expect_guard_failure_fd 7 'a closed and reopened unlocked descriptor'
exec 7>&-

exec 7<>"${LOCK}"
flock -x 7
mv "${LOCK}" "${tmp_dir}/replaced-release.lock"
install -o root -g root -m 0600 /dev/null "${LOCK}"
expect_guard_failure_fd 7 'a descriptor for the replaced lock path'
exec 7>&-
rm -f "${LOCK}"
mv "${tmp_dir}/replaced-release.lock" "${LOCK}"

"${PYTHON}" - "${LOCK}" "${FORMAL_GUARD}" <<'PY'
import fcntl
import os
import subprocess
import sys

lock, guard = sys.argv[1:]
descriptor = os.open(lock, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    duplicate = os.dup(descriptor)
    os.close(descriptor)
    try:
        completed = subprocess.run(
            ["/usr/bin/bash", guard, "--project", "drycontroller-edge", "--lock-fd", str(duplicate)],
            env={
                "HOME": "/tmp/attacker-home",
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "DOCKER_HOST": "tcp://127.0.0.1:9",
                "DOCKER_CONTEXT": "attacker",
                "DOCKER_CONFIG": "/tmp/attacker-config",
                "DOCKER_CLI_PLUGIN_EXTRA_DIRS": "/tmp/attacker-plugins",
            },
            pass_fds=(duplicate,),
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        os.close(duplicate)
finally:
    try:
        os.close(descriptor)
    except OSError:
        pass
if completed.returncode != 0:
    raise SystemExit(completed.stderr)
PY

rootfs="${tmp_dir}/rootfs"
install -d -o root -g root -m 0755 "${rootfs}"
sleep_binary="$(readlink -f /usr/bin/sleep)"
cp --parents "${sleep_binary}" "${rootfs}"
while IFS= read -r library; do
  [ -n "${library}" ] || continue
  cp --parents "${library}" "${rootfs}"
done < <(ldd "${sleep_binary}" | awk '{for (i=1; i<=NF; i++) if ($i ~ /^\//) print $i}')
tar -C "${rootfs}" -cf - . | docker_exact image import - "${protected_image}" >/dev/null
docker_exact image tag "${protected_image}" "${conflict_image}"
protected_id="$(docker_exact image inspect --format '{{.Id}}' "${protected_image}")"
[ "$(docker_exact image inspect --format '{{.Id}}' "${conflict_image}")" = "${protected_id}" ] \
  || fail 'fixture tag conflict identity changed'
tar -C "${rootfs}" -cf - . | docker_exact image import - "${task_image}" >/dev/null
owned_image_id="$(docker_exact image inspect --format '{{.Id}}' "${task_image}")"

volume_names=(
  postgres_data influxdb_data influxdb_config mosquitto_data mosquitto_logs
  mosquitto_secrets prometheus_data alertmanager_data
)
services=(
  'postgres|edge-postgres|/var/lib/postgresql/data'
  'influxdb|edge-influxdb|/var/lib/influxdb2'
  'mosquitto-init|edge-mosquitto-init|/mosquitto/secret'
  'mosquitto|edge-mosquitto|/mosquitto/data'
  'edge-api|edge-api|-'
  'edge-daq|edge-daq|-'
  'ai-softsensor|ai-softsensor|-'
  'ai-ctrl-optimizer|ai-ctrl-optimizer|-'
  'license-service|license-service|-'
  'edge-monitor|edge-monitor|-'
  'prometheus|edge-prometheus|/prometheus'
  'alertmanager|edge-alertmanager|/alertmanager'
)

exec 9<>"${LOCK}"
flock -x 9
for logical in "${volume_names[@]}"; do
  name="${PROJECT}_${logical}"
  docker_exact volume create \
    --label "com.docker.compose.project=${PROJECT}" \
    --label "com.docker.compose.volume=${logical}" "${name}" >/dev/null
  expect_guard_failure_fd 9 "existing volume ${logical}"
  docker_exact volume rm "${name}" >/dev/null
done
for entry in "${services[@]}"; do
  IFS='|' read -r service container destination <<<"${entry}"
  create_args=(
    container create --name "${container}"
    --label "com.docker.compose.project=${PROJECT}"
    --label "com.docker.compose.service=${service}"
  )
  if [ "${destination}" = '-' ]; then
    create_args+=(--mount 'type=tmpfs,dst=/tmp/rb8a2-guard')
  else
    bind_source="${tmp_dir}/bind-${service}"
    install -d -m 0755 "${bind_source}"
    create_args+=(--mount "type=bind,src=${bind_source},dst=${destination}")
  fi
  create_args+=("${task_image}" "${sleep_binary}" 300)
  docker_exact "${create_args[@]}" >/dev/null
  expect_guard_failure_fd 9 "existing service ${service}"
  docker_exact container rm "${container}" >/dev/null
done
exec 9>&-

cat >"${compose_file}" <<EOF
services:
  postgres: {image: ${task_image}, container_name: edge-postgres, command: ["${sleep_binary}", "300"], volumes: [postgres_data:/var/lib/postgresql/data]}
  influxdb: {image: ${task_image}, container_name: edge-influxdb, command: ["${sleep_binary}", "300"], volumes: [influxdb_data:/var/lib/influxdb2, influxdb_config:/etc/influxdb2]}
  mosquitto-init: {image: ${task_image}, container_name: edge-mosquitto-init, command: ["${sleep_binary}", "300"], volumes: [mosquitto_secrets:/mosquitto/secret]}
  mosquitto: {image: ${task_image}, container_name: edge-mosquitto, command: ["${sleep_binary}", "300"], volumes: [mosquitto_data:/mosquitto/data, mosquitto_logs:/mosquitto/log, mosquitto_secrets:/mosquitto/secret]}
  edge-api: {image: ${task_image}, container_name: edge-api, command: ["${sleep_binary}", "300"]}
  edge-daq: {image: ${task_image}, container_name: edge-daq, command: ["${sleep_binary}", "300"]}
  ai-softsensor: {image: ${task_image}, container_name: ai-softsensor, command: ["${sleep_binary}", "300"]}
  ai-ctrl-optimizer: {image: ${task_image}, container_name: ai-ctrl-optimizer, command: ["${sleep_binary}", "300"]}
  license-service: {image: ${task_image}, container_name: license-service, command: ["${sleep_binary}", "300"]}
  edge-monitor: {image: ${task_image}, container_name: edge-monitor, command: ["${sleep_binary}", "300"]}
  prometheus: {image: ${task_image}, container_name: edge-prometheus, command: ["${sleep_binary}", "300"], volumes: [prometheus_data:/prometheus]}
  alertmanager: {image: ${task_image}, container_name: edge-alertmanager, command: ["${sleep_binary}", "300"], volumes: [alertmanager_data:/alertmanager]}
volumes:
  postgres_data: {}
  influxdb_data: {}
  influxdb_config: {}
  mosquitto_data: {}
  mosquitto_logs: {}
  mosquitto_secrets: {}
  prometheus_data: {}
  alertmanager_data: {}
EOF

install -d -o root -g root -m 0755 /usr/local/bin
cat >/usr/local/bin/bash <<EOF
#!/bin/sh
printf invoked >'${malicious_bash_marker}'
exit 0
EOF
chmod 0755 /usr/local/bin/bash
path_wrapper_created=1
install -d -m 0700 "${malicious_config}" "${malicious_plugins}"
cat >"${malicious_config}/config.json" <<EOF
{"auths":{},"currentContext":"attacker","cli-plugins":{"extraDirs":["${malicious_plugins}"]},"plugins":{"compose":{"enabled":true}},"proxies":{"default":{"httpProxy":"http://127.0.0.1:9"}},"credsStore":"attacker"}
EOF
cat >"${malicious_plugins}/docker-compose" <<EOF
#!/bin/sh
printf invoked >'${malicious_plugin_marker}'
exit 0
EOF
chmod 0755 "${malicious_plugins}/docker-compose"

DOCKER_HOST='tcp://127.0.0.1:9' \
DOCKER_CONTEXT='attacker' \
DOCKER_CONFIG="${malicious_config}" \
DOCKER_CLI_PLUGIN_EXTRA_DIRS="${malicious_plugins}" \
PYTHONDONTWRITEBYTECODE=1 \
PATH='/usr/local/bin:/usr/bin:/bin' \
"${PYTHON}" - "${compose_file}" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, "/usr/libexec/drycontroller")
import host_release_engine

compose = Path(sys.argv[1])
engine = host_release_engine.HostReleaseEngine()
with engine._transaction_lock() as lock_descriptor:
    engine._assert_fresh_install(lock_descriptor)
    base = [
        host_release_engine._DOCKER_COMPOSE,
        "--project-name",
        "drycontroller-edge",
        "--file",
        str(compose),
    ]
    engine._run_command([*base, "config", "--quiet"], compose.parent, None)
    engine._run_command([*base, "up", "--detach", "--pull", "never", "--no-build"], compose.parent, None)
PY
compose_started=1

[ ! -e "${malicious_bash_marker}" ] || fail '/usr/local/bin/bash bypassed the fixed interpreter'
[ ! -e "${malicious_plugin_marker}" ] || fail 'malicious Compose plugin was dynamically loaded'
[ "$(docker_exact container ls --all --quiet --no-trunc | wc -l)" -eq 12 ] \
  || fail 'formal Compose chain did not create all 12 service containers'
[ "$(docker_exact volume ls --quiet --filter "label=com.docker.compose.project=${PROJECT}" | wc -l)" -eq 8 ] \
  || fail 'formal Compose chain did not create all 8 persistent volumes'
[ "$(docker_exact image inspect --format '{{.Id}}' "${protected_image}")" = "${protected_id}" ] \
  || fail 'non-task protected image was deleted or changed'
[ "$(docker_exact image inspect --format '{{.Id}}' "${conflict_image}")" = "${protected_id}" ] \
  || fail 'conflicting non-task image tag was deleted or changed'

exec 9<>"${LOCK}"
flock -x 9
expect_guard_failure_fd 9 'the formally started data plane'
exec 9>&-

printf 'fresh-install real Docker E2E passed: self-daemon=1 formal-chain=install,guard,compose,start volumes=%d services=%d bash-spoof=refused plugin-context=refused wrong-daemon=refused lock-matrix=wrong-inode,shared,reopen,replace,dup-pass-fds image-conflict=preserved\n' \
  "${#volume_names[@]}" "${#services[@]}"
