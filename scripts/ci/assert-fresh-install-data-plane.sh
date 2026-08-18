#!/usr/bin/env bash

set -euo pipefail

readonly FORMAL_PROJECT='drycontroller-edge'
readonly DOCKER='/usr/bin/docker'
readonly PYTHON='/usr/bin/python3'
readonly RELEASE_LOCK='/var/lib/drycontroller-ota/release.lock'

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
    "${DOCKER}" volume inspect --format "${template}" "${volume_name}" 2>/dev/null
  )"; then
    deny "unable to inspect volume ${volume_name}"
  fi
  [ -n "${INSPECTED_FIELD}" ] || deny "empty volume inspect output for ${volume_name}"
}

inspect_container_field() {
  local container_id="$1"
  local template="$2"
  if ! INSPECTED_FIELD="$(
    "${DOCKER}" container inspect --format "${template}" "${container_id}" 2>/dev/null
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
if ! "${PYTHON}" - "${lock_fd}" "${RELEASE_LOCK}" "${DOCKER}" <<'PY'
import fcntl
import os
import stat
import sys

try:
    descriptor = int(sys.argv[1])
    lock_path = sys.argv[2]
    docker_path = sys.argv[3]
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

    executable = os.lstat(docker_path)
    parent = os.lstat(os.path.dirname(docker_path))
    if (
        not stat.S_ISREG(executable.st_mode)
        or executable.st_uid != os.geteuid()
        or executable.st_mode & 0o022
        or not executable.st_mode & 0o111
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or parent.st_mode & 0o022
    ):
        raise OSError("Docker executable is not trusted")
except (OSError, ValueError, IndexError):
    raise SystemExit(1)
PY
then
  deny 'inherited release lock or fixed Docker executable is untrusted'
fi

if ! docker_version="$("${DOCKER}" version --format '{{.Server.Version}}' 2>/dev/null)"; then
  deny 'unable to query Docker daemon version'
fi
if [[ ! "${docker_version}" =~ ^[0-9]+(\.[0-9]+){1,3}([+~-][0-9A-Za-z._-]+)?$ ]]; then
  deny 'Docker daemon returned an empty or malformed version'
fi

if ! volume_names="$("${DOCKER}" volume ls --quiet 2>/dev/null)"; then
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

if ! container_ids="$("${DOCKER}" container ls --all --quiet --no-trunc 2>/dev/null)"; then
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
    "${DOCKER}" container inspect \
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
