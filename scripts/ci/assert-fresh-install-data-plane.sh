#!/usr/bin/env bash

set -euo pipefail

deny() {
  printf 'fresh-install preflight refused: %s\n' "$1" >&2
  exit 1
}

is_target_volume_name() {
  case "$1" in
    postgres_data|influxdb_data|influxdb_config|prometheus_data|\
      *_postgres_data|*_influxdb_data|*_influxdb_config|*_prometheus_data)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

is_target_service() {
  case "$1" in
    postgres|influxdb|prometheus)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

is_target_container_name() {
  case "$1" in
    edge-postgres|edge-influxdb|edge-prometheus)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

is_target_mount() {
  case "$1" in
    /var/lib/postgresql/data|/var/lib/influxdb2|/etc/influxdb2|/prometheus)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

normalize_label() {
  case "${INSPECTED_LABEL}" in
    ''|'<no value>') INSPECTED_LABEL='' ;;
  esac
}

inspect_volume_label() {
  local volume_name="$1"
  local label_name="$2"
  if ! INSPECTED_LABEL="$(
    docker volume inspect --format "{{ index .Labels \"${label_name}\" }}" "${volume_name}" 2>/dev/null
  )"; then
    deny "unable to inspect volume ${volume_name}"
  fi
  normalize_label
}

inspect_container_field() {
  local container_id="$1"
  local template="$2"
  if ! INSPECTED_FIELD="$(
    docker container inspect --format "${template}" "${container_id}" 2>/dev/null
  )"; then
    deny "unable to inspect container ${container_id}"
  fi
}

inspect_container_label() {
  local container_id="$1"
  local label_name="$2"
  inspect_container_field "${container_id}" "{{ index .Config.Labels \"${label_name}\" }}"
  INSPECTED_LABEL="${INSPECTED_FIELD}"
  normalize_label
}

require_compose_ownership() {
  local resource="$1"
  local project_label="$2"
  local object_label="$3"

  if [ -z "${project_label}" ] && [ -z "${object_label}" ]; then
    deny "unknown Compose ownership labels on ${resource}"
  fi
  if [ -z "${project_label}" ] || [ -z "${object_label}" ]; then
    deny "incomplete Compose ownership labels on ${resource}"
  fi
  if [[ ! "${project_label}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || \
    [[ ! "${object_label}" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
    deny "invalid Compose ownership labels on ${resource}"
  fi
}

project_name="${1:-drycontroller-edge}"
if [[ ! "${project_name}" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
  deny "invalid Compose project ${project_name}"
fi
command -v docker >/dev/null 2>&1 || deny 'docker CLI is unavailable'

if ! docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
  deny 'unable to query Docker daemon version'
fi

if ! volume_names="$(docker volume ls --quiet 2>/dev/null)"; then
  deny 'unable to list Docker volumes'
fi
while IFS= read -r volume_name; do
  [ -n "${volume_name}" ] || continue
  if [[ ! "${volume_name}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
    deny 'Docker returned an invalid volume name'
  fi
  if is_target_volume_name "${volume_name}"; then
    deny "existing target volume ${volume_name}"
  fi

  inspect_volume_label "${volume_name}" 'com.docker.compose.project'
  volume_project="${INSPECTED_LABEL}"
  inspect_volume_label "${volume_name}" 'com.docker.compose.volume'
  volume_object="${INSPECTED_LABEL}"
  require_compose_ownership "volume ${volume_name}" "${volume_project}" "${volume_object}"
  if is_target_volume_name "${volume_object}"; then
    deny "existing target volume ${volume_name} (${volume_project}/${volume_object})"
  fi
done <<<"${volume_names}"

if ! container_ids="$(docker container ls --all --quiet --no-trunc 2>/dev/null)"; then
  deny 'unable to list Docker containers'
fi
while IFS= read -r container_id; do
  [ -n "${container_id}" ] || continue
  if [[ ! "${container_id}" =~ ^[a-f0-9]{12,64}$ ]]; then
    deny 'Docker returned an invalid container ID'
  fi

  inspect_container_field "${container_id}" '{{.Name}}'
  container_name="${INSPECTED_FIELD#/}"
  if [[ ! "${container_name}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
    deny "Docker returned an invalid name for container ${container_id}"
  fi
  if is_target_container_name "${container_name}"; then
    deny "existing target container ${container_name} (${container_id})"
  fi

  inspect_container_label "${container_id}" 'com.docker.compose.project'
  container_project="${INSPECTED_LABEL}"
  inspect_container_label "${container_id}" 'com.docker.compose.service'
  container_service="${INSPECTED_LABEL}"
  require_compose_ownership "container ${container_name}" "${container_project}" "${container_service}"
  if is_target_service "${container_service}"; then
    deny "existing target container ${container_name} (${container_project}/${container_service})"
  fi

  inspect_container_field "${container_id}" '{{ range .Mounts }}{{ println .Destination }}{{ end }}'
  while IFS= read -r destination; do
    [ -n "${destination}" ] || continue
    [[ "${destination}" == /* ]] || deny "invalid mount metadata on container ${container_name}"
    if is_target_mount "${destination}"; then
      deny "existing target container ${container_name} mounts ${destination}"
    fi
  done <<<"${INSPECTED_FIELD}"
done <<<"${container_ids}"

printf 'fresh-install data-plane preflight passed for project %s\n' "${project_name}"
