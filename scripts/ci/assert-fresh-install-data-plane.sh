#!/usr/bin/bash

set -euo pipefail

readonly FORMAL_PROJECT='drycontroller-edge'
readonly DOCKER='/usr/bin/docker'
readonly PYTHON='/usr/bin/python3'
readonly RELEASE_LOCK='/var/lib/drycontroller-ota/release.lock'
readonly DOCKER_CONFIG_DIR='/var/lib/drycontroller-ota/guard-docker-config'
readonly TOOL_IDENTITY='/etc/drycontroller/runtime-tools.json'
readonly DAEMON_IDENTITY='/etc/drycontroller/docker-daemon.json'
readonly DOCKER_SOCKET='/run/docker.sock'
readonly TRUST_ROOT='/'
readonly SOCKET_TRUST_ROOT='/'
readonly DOCKER_SOCKET_KIND='socket'

export HOME='/var/empty'
export PATH='/usr/bin:/bin'
export DOCKER_HOST='unix:///run/docker.sock'
export DOCKER_CONFIG="${DOCKER_CONFIG_DIR}"
unset DOCKER_CONTEXT DOCKER_CLI_PLUGIN_EXTRA_DIRS DOCKER_CERT_PATH DOCKER_TLS_VERIFY \
  DOCKER_API_VERSION COMPOSE_FILE COMPOSE_PATH_SEPARATOR COMPOSE_PROFILES

deny() {
  printf 'fresh-install preflight refused: %s\n' "$1" >&2
  exit 1
}

known_volume() {
  case "$1" in
    postgres_data|influxdb_data|influxdb_config|mosquitto_data|mosquitto_logs|\
      mosquitto_secrets|prometheus_data|alertmanager_data)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

container_name_for_service() {
  case "$1" in
    postgres) EXPECTED_CONTAINER='edge-postgres' ;;
    influxdb) EXPECTED_CONTAINER='edge-influxdb' ;;
    mosquitto-init) EXPECTED_CONTAINER='edge-mosquitto-init' ;;
    mosquitto) EXPECTED_CONTAINER='edge-mosquitto' ;;
    edge-api) EXPECTED_CONTAINER='edge-api' ;;
    edge-daq) EXPECTED_CONTAINER='edge-daq' ;;
    ai-softsensor) EXPECTED_CONTAINER='ai-softsensor' ;;
    ai-ctrl-optimizer) EXPECTED_CONTAINER='ai-ctrl-optimizer' ;;
    license-service) EXPECTED_CONTAINER='license-service' ;;
    edge-monitor) EXPECTED_CONTAINER='edge-monitor' ;;
    prometheus) EXPECTED_CONTAINER='edge-prometheus' ;;
    alertmanager) EXPECTED_CONTAINER='edge-alertmanager' ;;
    *) return 1 ;;
  esac
}

volume_for_destination() {
  case "$1" in
    /var/lib/postgresql/data) EXPECTED_VOLUME='postgres_data' ;;
    /var/lib/influxdb2) EXPECTED_VOLUME='influxdb_data' ;;
    /etc/influxdb2) EXPECTED_VOLUME='influxdb_config' ;;
    /mosquitto/data) EXPECTED_VOLUME='mosquitto_data' ;;
    /mosquitto/log) EXPECTED_VOLUME='mosquitto_logs' ;;
    /mosquitto/secret) EXPECTED_VOLUME='mosquitto_secrets' ;;
    /prometheus) EXPECTED_VOLUME='prometheus_data' ;;
    /alertmanager) EXPECTED_VOLUME='alertmanager_data' ;;
    *) return 1 ;;
  esac
}

required_destinations() {
  case "$1" in
    postgres) printf '%s\n' '/var/lib/postgresql/data' ;;
    influxdb) printf '%s\n' '/var/lib/influxdb2' '/etc/influxdb2' ;;
    mosquitto-init) printf '%s\n' '/mosquitto/secret' ;;
    mosquitto) printf '%s\n' '/mosquitto/data' '/mosquitto/log' '/mosquitto/secret' ;;
    prometheus) printf '%s\n' '/prometheus' ;;
    alertmanager) printf '%s\n' '/alertmanager' ;;
  esac
}

inspect_volume_field() {
  local volume_name="$1"
  local template="$2"
  if ! INSPECTED_FIELD="$(
    docker_checked volume inspect --format "${template}" "${volume_name}" 2>/dev/null
  )"; then
    deny "unable to inspect volume ${volume_name}"
  fi
  [ -n "${INSPECTED_FIELD}" ] || deny "empty volume inspect output for ${volume_name}"
}

inspect_container_field() {
  local container_id="$1"
  local template="$2"
  if ! INSPECTED_FIELD="$(
    docker_checked container inspect --format "${template}" "${container_id}" 2>/dev/null
  )"; then
    deny "unable to inspect container ${container_id}"
  fi
  [ -n "${INSPECTED_FIELD}" ] || deny "empty container inspect output for ${container_id}"
}

project_name=''
lock_fd=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    --project)
      [ "$#" -ge 2 ] || deny '--project requires a value'
      project_name="$2"
      shift 2
      ;;
    --lock-fd)
      [ "$#" -ge 2 ] || deny '--lock-fd requires a value'
      lock_fd="$2"
      shift 2
      ;;
    *)
      deny "unknown argument $1"
      ;;
  esac
done

[ "${project_name}" = "${FORMAL_PROJECT}" ] \
  || deny "Compose project mismatch: expected ${FORMAL_PROJECT}"
[[ "${lock_fd}" =~ ^[0-9]+$ ]] || deny 'inherited release transaction lock is required'
[ -x "${PYTHON}" ] || deny 'fixed Python runtime is unavailable'
exec 8<"${DOCKER}" || deny 'fixed Docker executable is unavailable'
readonly DOCKER_FD=8
readonly DOCKER_EXEC="/proc/self/fd/${DOCKER_FD}"
if ! expected_daemon_identity="$("${PYTHON}" - "${lock_fd}" "${RELEASE_LOCK}" "${DOCKER}" "${DOCKER_CONFIG_DIR}" "${TOOL_IDENTITY}" "${DAEMON_IDENTITY}" "${DOCKER_SOCKET}" "${DOCKER_FD}" "${TRUST_ROOT}" "${SOCKET_TRUST_ROOT}" "${DOCKER_SOCKET_KIND}" <<'PY'
import fcntl
import hashlib
import json
import os
import stat
import sys

def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result

try:
    descriptor = int(sys.argv[1])
    lock_path = sys.argv[2]
    docker_path, docker_config_dir, tool_identity, daemon_identity, socket_path = sys.argv[3:8]
    docker_descriptor = int(sys.argv[8])
    trust_root = sys.argv[9]
    socket_trust_root = sys.argv[10]
    socket_kind = sys.argv[11]
    opened = os.fstat(descriptor)
    expected = os.lstat(lock_path)
    if not stat.S_ISREG(expected.st_mode) or stat.S_IMODE(expected.st_mode) != 0o600:
        raise OSError("release lock metadata invalid")
    if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
        raise OSError("release lock descriptor mismatch")
    if sys.platform.startswith("linux"):
        with open(f"/proc/self/fdinfo/{descriptor}", encoding="utf-8") as stream:
            lock_fields = [
                line.split()
                for line in stream
                if line.startswith("lock:")
            ]
        if not any(fields[2:5] == ["FLOCK", "ADVISORY", "WRITE"] for fields in lock_fields):
            raise OSError("release lock descriptor is not held")
    else:
        probe = os.open(
            lock_path,
            os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                fcntl.flock(probe, fcntl.LOCK_UN)
                raise OSError("release lock descriptor is not held")
        finally:
            os.close(probe)

    def validate_ancestors(value, root=trust_root):
        current = os.path.dirname(value)
        while True:
            ancestor = os.lstat(current)
            if (
                not stat.S_ISDIR(ancestor.st_mode)
                or ancestor.st_uid != os.geteuid()
                or ancestor.st_mode & 0o022
            ):
                raise OSError("untrusted ancestor")
            if current == root:
                break
            if current == "/":
                raise OSError("path outside trusted root")
            current = os.path.dirname(current)

    validate_ancestors(docker_path)
    executable = os.lstat(docker_path)
    opened_executable = os.fstat(docker_descriptor)
    if (
        not stat.S_ISREG(executable.st_mode)
        or executable.st_uid != os.geteuid()
        or executable.st_mode & 0o022
        or not executable.st_mode & 0o111
        or executable.st_nlink != 1
        or (opened_executable.st_dev, opened_executable.st_ino)
        != (executable.st_dev, executable.st_ino)
    ):
        raise OSError("Docker executable is not trusted")
    digest = hashlib.sha256()
    while True:
        chunk = os.read(docker_descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(docker_descriptor, 0, os.SEEK_SET)

    def read_identity(path, schema):
        validate_ancestors(path)
        metadata = os.lstat(path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or metadata.st_nlink != 1
        ):
            raise OSError("runtime identity metadata invalid")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            opened = os.fstat(descriptor)
            raw = os.read(descriptor, 65537)
        finally:
            os.close(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or not raw
            or len(raw) > 65536
        ):
            raise OSError("runtime identity changed")
        payload = json.loads(raw, object_pairs_hook=strict_object)
        if not isinstance(payload, dict) or payload.get("schema") != schema:
            raise OSError("runtime identity schema invalid")
        return payload

    tools = read_identity(tool_identity, "drycontroller.runtime-tools.v1")
    records = tools.get("executables")
    if (
        not isinstance(records, dict)
        or set(records) != {docker_path, "/usr/bin/bash", "/usr/libexec/docker/cli-plugins/docker-compose"}
        or records.get(docker_path) != {"sha256": f"sha256:{digest.hexdigest()}"}
    ):
        raise OSError("Docker executable content is untrusted")

    config_directory = os.lstat(docker_config_dir)
    config_path = os.path.join(docker_config_dir, "config.json")
    config_file = os.lstat(config_path)
    if (
        os.path.realpath(docker_config_dir) != docker_config_dir
        or os.path.realpath(config_path) != config_path
        or not stat.S_ISDIR(config_directory.st_mode)
        or config_directory.st_uid != os.geteuid()
        or stat.S_IMODE(config_directory.st_mode) != 0o500
        or not stat.S_ISREG(config_file.st_mode)
        or config_file.st_uid != os.geteuid()
        or stat.S_IMODE(config_file.st_mode) != 0o400
        or config_file.st_nlink != 1
    ):
        raise OSError("guard Docker config is not trusted")
    descriptor = os.open(config_path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        raw = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    if (
        (opened.st_dev, opened.st_ino) != (config_file.st_dev, config_file.st_ino)
        or len(raw) > 4096
        or json.loads(raw, object_pairs_hook=strict_object) != {"auths": {}}
    ):
        raise OSError("guard Docker config is invalid")
    validate_ancestors(socket_path, socket_trust_root)
    socket_metadata = os.lstat(socket_path)
    if (
        (socket_kind == "socket" and not stat.S_ISSOCK(socket_metadata.st_mode))
        or (socket_kind == "regular-test" and not stat.S_ISREG(socket_metadata.st_mode))
        or socket_kind not in {"socket", "regular-test"}
        or socket_metadata.st_uid != os.geteuid()
        or socket_metadata.st_mode & 0o002
        or socket_metadata.st_nlink != 1
    ):
        raise OSError("Docker socket is untrusted")
    daemon = read_identity(daemon_identity, "drycontroller.docker-daemon.v1")
    if set(daemon) != {"schema", "id", "name", "root_dir"} or not all(
        isinstance(daemon.get(key), str) and daemon[key]
        for key in ("id", "name", "root_dir")
    ):
        raise OSError("Docker daemon identity invalid")
    print(daemon["id"], daemon["name"], daemon["root_dir"], sep="\n")
except (OSError, UnicodeError, ValueError, IndexError):
    raise SystemExit(1)
PY
  )"; then
  deny 'inherited release lock or fixed Docker executable is untrusted'
fi
readonly expected_daemon_identity
readonly socket_identity="$(stat -c '%d:%i' "${DOCKER_SOCKET}")"

docker_checked() {
  local output
  local observed
  local status=0
  [ "$(stat -c '%d:%i' "${DOCKER_SOCKET}" 2>/dev/null || true)" = "${socket_identity}" ] \
    || deny 'Docker socket identity changed'
  output="$("${DOCKER_EXEC}" "$@")" || status=$?
  observed="$("${DOCKER_EXEC}" info --format '{{.ID}}\n{{.Name}}\n{{.DockerRootDir}}')" \
    || deny 'Docker daemon identity unavailable'
  [ "${observed}" = "${expected_daemon_identity}" ] \
    || deny 'Docker daemon identity changed'
  [ "$(stat -c '%d:%i' "${DOCKER_SOCKET}" 2>/dev/null || true)" = "${socket_identity}" ] \
    || deny 'Docker socket identity changed'
  [ "${status}" -eq 0 ] || return "${status}"
  printf '%s' "${output}"
}

if ! docker_version="$(docker_checked version --format '{{.Server.Version}}' 2>/dev/null)"; then
  deny 'unable to query Docker daemon version'
fi
if [[ ! "${docker_version}" =~ ^[0-9]+(\.[0-9]+){1,3}([+~-][0-9A-Za-z._-]+)?$ ]]; then
  deny 'Docker daemon returned an empty or malformed version'
fi

if ! volume_names="$(docker_checked volume ls --quiet 2>/dev/null)"; then
  deny 'unable to list Docker volumes'
fi
while IFS= read -r volume_name; do
  [ -n "${volume_name}" ] || continue
  if [[ ! "${volume_name}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
    deny 'Docker returned a malformed volume list'
  fi

  inspect_volume_field "${volume_name}" '{{.Name}}'
  inspected_name="${INSPECTED_FIELD}"
  [ "${inspected_name}" = "${volume_name}" ] \
    || deny "volume inventory/inspect name conflict for ${volume_name}"
  inspect_volume_field "${volume_name}" '{{ index .Labels "com.docker.compose.project" }}'
  volume_project="${INSPECTED_FIELD}"
  inspect_volume_field "${volume_name}" '{{ index .Labels "com.docker.compose.volume" }}'
  volume_logical="${INSPECTED_FIELD}"
  if [ "${volume_project}" = '<no value>' ] || [ "${volume_logical}" = '<no value>' ]; then
    deny "unknown Compose ownership labels on volume ${volume_name}"
  fi
  if [[ ! "${volume_project}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || \
    [[ ! "${volume_logical}" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
    deny "malformed Compose ownership labels on volume ${volume_name}"
  fi
  [ "${volume_project}" = "${project_name}" ] \
    || deny "volume project mismatch on ${volume_name}"
  known_volume "${volume_logical}" \
    || deny "unknown Compose volume label on ${volume_name}"
  [ "${volume_name}" = "${project_name}_${volume_logical}" ] \
    || deny "volume name/label conflict on ${volume_name}"
  deny "existing target volume ${volume_name}"
done <<<"${volume_names}"

if ! container_ids="$(docker_checked container ls --all --quiet --no-trunc 2>/dev/null)"; then
  deny 'unable to list Docker containers'
fi
while IFS= read -r container_id; do
  [ -n "${container_id}" ] || continue
  if [[ ! "${container_id}" =~ ^[a-f0-9]{64}$ ]]; then
    deny 'Docker returned a malformed container list'
  fi

  inspect_container_field "${container_id}" '{{.Id}}'
  [ "${INSPECTED_FIELD}" = "${container_id}" ] \
    || deny "container inventory/inspect ID conflict for ${container_id}"
  inspect_container_field "${container_id}" '{{.Name}}'
  [[ "${INSPECTED_FIELD}" =~ ^/[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] \
    || deny "malformed container name for ${container_id}"
  container_name="${INSPECTED_FIELD#/}"
  inspect_container_field "${container_id}" '{{ index .Config.Labels "com.docker.compose.project" }}'
  container_project="${INSPECTED_FIELD}"
  inspect_container_field "${container_id}" '{{ index .Config.Labels "com.docker.compose.service" }}'
  container_service="${INSPECTED_FIELD}"
  if [ "${container_project}" = '<no value>' ] || [ "${container_service}" = '<no value>' ]; then
    deny "unknown Compose ownership labels on container ${container_name}"
  fi
  if [[ ! "${container_project}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || \
    [[ ! "${container_service}" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
    deny "malformed Compose ownership labels on container ${container_name}"
  fi
  [ "${container_project}" = "${project_name}" ] \
    || deny "container project mismatch on ${container_name}"
  container_name_for_service "${container_service}" \
    || deny "unknown Compose service on container ${container_name}"
  [ "${container_name}" = "${EXPECTED_CONTAINER}" ] \
    || deny "container name/service conflict on ${container_name}"

  if ! mount_lines="$(
    docker_checked container inspect \
      --format '{{ range .Mounts }}{{ printf "%s|%s|%s\n" .Type .Name .Destination }}{{ end }}' \
      "${container_id}" 2>/dev/null
  )"; then
    deny "unable to inspect mounts for container ${container_name}"
  fi
  seen_destinations='|'
  while IFS= read -r mount_line; do
    [ -n "${mount_line}" ] || continue
    IFS='|' read -r mount_type mount_name destination extra <<<"${mount_line}"
    if [ -n "${extra:-}" ] || [ -z "${mount_type}" ] || [[ ! "${destination}" == /* ]]; then
      deny "malformed mount metadata on container ${container_name}"
    fi
    case "${mount_type}" in
      bind|tmpfs) ;;
      volume)
        [[ "${mount_name}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] \
          || deny "malformed volume mount on container ${container_name}"
        ;;
      *) deny "unknown mount type on container ${container_name}" ;;
    esac
    if volume_for_destination "${destination}"; then
      if [ "${mount_type}" != 'volume' ] || \
        [ "${mount_name}" != "${project_name}_${EXPECTED_VOLUME}" ]; then
        deny "persistent mount conflict on container ${container_name}: ${destination}"
      fi
      case "${seen_destinations}" in
        *"|${destination}|"*) deny "duplicate persistent mount on container ${container_name}" ;;
      esac
      seen_destinations="${seen_destinations}${destination}|"
    elif [ "${mount_type}" = 'volume' ]; then
      deny "unknown persistent mount on container ${container_name}: ${destination}"
    fi
  done <<<"${mount_lines}"
  while IFS= read -r required_destination; do
    [ -n "${required_destination}" ] || continue
    case "${seen_destinations}" in
      *"|${required_destination}|"*) ;;
      *) deny "persistent mount missing on container ${container_name}: ${required_destination}" ;;
    esac
  done < <(required_destinations "${container_service}")
  deny "existing target container ${container_name}"
done <<<"${container_ids}"

printf 'fresh-install data-plane preflight passed for project %s\n' "${project_name}"
