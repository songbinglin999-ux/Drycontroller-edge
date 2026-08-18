#!/usr/bin/env bash

set -euo pipefail

readonly DOCKER='/usr/bin/docker'
readonly GUARD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/assert-fresh-install-data-plane.sh"
readonly LOCK='/var/lib/drycontroller-ota/release.lock'
readonly ISOLATION_MARKER='/etc/drycontroller-rb8a2-isolated-daemon'
readonly PROJECT='drycontroller-edge'
readonly IMAGE='drycontroller.local/rb8a2-fresh-guard-e2e:fixture'

fail() {
  printf 'fresh-install real Docker E2E failed: %s\n' "$1" >&2
  exit 1
}

[ "${EUID}" -eq 0 ] || fail 'must run as root inside an isolated disposable daemon environment'
[ "${DRYCONTROLLER_FRESH_GUARD_ISOLATED_DAEMON:-}" = '1' ] \
  || fail 'isolated daemon acknowledgement is required'
expected_daemon="${DRYCONTROLLER_FRESH_GUARD_DAEMON_NAME:-}"
[ -n "${expected_daemon}" ] || fail 'expected isolated daemon name is required'
[ -f "${ISOLATION_MARKER}" ] || fail 'root-only isolation marker is absent'
[ "$(stat -c '%u:%a' "${ISOLATION_MARKER}")" = '0:400' ] \
  || fail 'isolation marker ownership or mode is invalid'
[ "$(cat "${ISOLATION_MARKER}")" = "${expected_daemon}" ] \
  || fail 'isolation marker does not identify this daemon'
[ -x "${DOCKER}" ] || fail 'fixed Docker executable is absent'
[ "$("${DOCKER}" info --format '{{.Name}}')" = "${expected_daemon}" ] \
  || fail 'Docker daemon identity mismatch'
[ -z "$("${DOCKER}" container ls --all --quiet --no-trunc)" ] \
  || fail 'isolated daemon is not container-empty before the test'
[ -z "$("${DOCKER}" volume ls --quiet)" ] \
  || fail 'isolated daemon is not volume-empty before the test'
[ ! -e /usr/local/bin/docker ] || fail '/usr/local/bin/docker already exists in the isolated environment'

tmp_dir="$(mktemp -d /tmp/drycontroller-rb8a2-guard.XXXXXX)"
fake_path_created=0
container_names=(
  edge-postgres edge-influxdb edge-mosquitto-init edge-mosquitto edge-api edge-daq
  ai-softsensor ai-ctrl-optimizer license-service edge-monitor edge-prometheus edge-alertmanager
)
volume_names=(
  postgres_data influxdb_data influxdb_config mosquitto_data mosquitto_logs
  mosquitto_secrets prometheus_data alertmanager_data
)

cleanup() {
  local name
  set +e
  for name in "${container_names[@]}"; do
    "${DOCKER}" container rm "${name}" >/dev/null 2>&1 || true
  done
  for name in "${volume_names[@]}"; do
    "${DOCKER}" volume rm "${PROJECT}_${name}" >/dev/null 2>&1 || true
  done
  "${DOCKER}" image rm "${IMAGE}" >/dev/null 2>&1 || true
  if [ "${fake_path_created}" -eq 1 ]; then
    rm -f /usr/local/bin/docker
  fi
  rm -rf "${tmp_dir}"
}
trap cleanup EXIT

install -d -o root -g root -m 0700 "$(dirname "${LOCK}")"
touch "${LOCK}"
chown root:root "${LOCK}"
chmod 0600 "${LOCK}"
exec 8<>"${LOCK}"
if "${GUARD}" --project "${PROJECT}" --lock-fd 8 >/dev/null 2>&1; then
  fail 'guard accepted the production lock inode through an unlocked descriptor'
fi
exec 8>&-
exec 9<>"${LOCK}"
flock -x 9

run_guard() {
  "${GUARD}" --project "${PROJECT}" --lock-fd 9
}

expect_guard_failure() {
  local context="$1"
  local output
  if output="$(run_guard 2>&1)"; then
    fail "guard accepted existing ${context}"
  fi
  case "${output}" in
    *'fresh-install preflight refused:'*) ;;
    *) fail "guard returned an unexpected failure for ${context}" ;;
  esac
}

run_guard >/dev/null || fail 'empty isolated daemon was refused'

install -d -o root -g root -m 0755 /usr/local/bin
cat > /usr/local/bin/docker <<'FAKE'
#!/bin/sh
case "${1:-}" in
  version) printf '%s\n' '99.0.0' ;;
esac
exit 0
FAKE
chmod 0755 /usr/local/bin/docker
fake_path_created=1
export PATH="/usr/local/bin:/usr/bin:/bin"

for logical in "${volume_names[@]}"; do
  name="${PROJECT}_${logical}"
  "${DOCKER}" volume create \
    --label "com.docker.compose.project=${PROJECT}" \
    --label "com.docker.compose.volume=${logical}" \
    "${name}" >/dev/null
  expect_guard_failure "volume ${logical}"
  "${DOCKER}" volume rm "${name}" >/dev/null
done

mkdir -p "${tmp_dir}/rootfs"
printf '%s\n' fixture >"${tmp_dir}/rootfs/fixture"
tar -C "${tmp_dir}/rootfs" -cf - fixture | "${DOCKER}" image import - "${IMAGE}" >/dev/null

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
for entry in "${services[@]}"; do
  IFS='|' read -r service container destination <<<"${entry}"
  create_args=(
    container create
    --name "${container}"
    --label "com.docker.compose.project=${PROJECT}"
    --label "com.docker.compose.service=${service}"
  )
  if [ "${destination}" = '-' ]; then
    create_args+=(--mount 'type=tmpfs,dst=/tmp/rb8a2-guard')
  else
    bind_source="${tmp_dir}/bind-${service}"
    mkdir -p "${bind_source}"
    create_args+=(--mount "type=bind,src=${bind_source},dst=${destination}")
  fi
  create_args+=("${IMAGE}" /bin/true)
  "${DOCKER}" "${create_args[@]}" >/dev/null
  expect_guard_failure "service ${service}"
  "${DOCKER}" container rm "${container}" >/dev/null
done

cleanup
trap - EXIT
set -e
[ -z "$("${DOCKER}" container ls --all --quiet --no-trunc)" ] \
  || fail 'isolated test containers remain after cleanup'
[ -z "$("${DOCKER}" volume ls --quiet)" ] \
  || fail 'isolated test volumes remain after cleanup'
if "${DOCKER}" image inspect "${IMAGE}" >/dev/null 2>&1; then
  fail 'isolated test image remains after cleanup'
fi
[ ! -e /usr/local/bin/docker ] || fail 'PATH-spoof Docker remains after cleanup'
printf 'fresh-install real Docker E2E passed: empty=1 volumes=%d services=%d path-spoof=refused unlocked-fd=refused\n' \
  "${#volume_names[@]}" "${#services[@]}"
