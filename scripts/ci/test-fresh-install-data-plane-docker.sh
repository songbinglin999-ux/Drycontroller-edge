#!/usr/bin/bash

# Runs only inside the privileged, disposable Linux CI job image. The fixture is
# a completed production-topology H1/S1 pair plus a completed S2 policy backed by
# an authenticated TLS registry. No host Docker socket or data root is mounted.
set -euo pipefail

readonly DOCKER='/usr/bin/docker'
readonly DOCKERD='/usr/bin/dockerd'
readonly COMPOSE='/usr/libexec/docker/cli-plugins/docker-compose'
readonly BASH='/usr/bin/bash'
readonly PYTHON='/usr/bin/python3'
readonly SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly FIXTURE_ROOT="${DRYCONTROLLER_FORMAL_INSTALL_FIXTURE_DIR:-}"
readonly ISOLATION_MARKER='/etc/drycontroller-rb8a2-isolated-daemon'
readonly STATE_ROOT='/var/lib/drycontroller-ota'
readonly GUARD_CONFIG="${STATE_ROOT}/guard-docker-config"
readonly AUTH_SOURCE="${STATE_ROOT}/registry-auth.json"
readonly REGISTRY_CONTAINER='drycontroller-rb8a2-registry'
readonly REGISTRY_IMAGE='registry:2'
readonly CLOUD_ADDRESS='100.100.0.2'
readonly CLOUD_PORT='443'
readonly PLC_FIXTURE_ADDRESS='192.168.1.100'

fail() {
  printf 'formal-install private OTA E2E failed: %s\n' "$1" >&2
  exit 1
}

[ "${EUID}" -eq 0 ] || fail 'must run as root in the disposable Linux job'
[ "$(uname -s)" = 'Linux' ] || fail 'Linux is required'
[ "${DRYCONTROLLER_FRESH_GUARD_ISOLATED_DAEMON:-}" = '1' ] \
  || fail 'isolated daemon acknowledgement is required'
[ -n "${FIXTURE_ROOT}" ] && [ -d "${FIXTURE_ROOT}" ] && [ ! -L "${FIXTURE_ROOT}" ] \
  || fail 'formal signed fixture directory is required'
[ -f "${ISOLATION_MARKER}" ] && [ ! -L "${ISOLATION_MARKER}" ] \
  || fail 'root-only isolation marker is absent'
[ "$(stat -c '%u:%a' "${ISOLATION_MARKER}")" = '0:400' ] \
  || fail 'isolation marker ownership or mode is invalid'
[ -x "${DOCKER}" ] && [ ! -L "${DOCKER}" ] || fail 'fixed Docker executable is unavailable'
[ -x "${DOCKERD}" ] && [ ! -L "${DOCKERD}" ] || fail 'fixed dockerd executable is unavailable'
[ -x "${COMPOSE}" ] && [ ! -L "${COMPOSE}" ] || fail 'fixed Compose executable is unavailable'
[ -x "${BASH}" ] && [ ! -L "${BASH}" ] || fail 'fixed Bash executable is unavailable'
[ -x "${PYTHON}" ] || fail 'fixed Python executable is unavailable'
[ ! -e /run/docker.sock ] || fail 'an existing Docker socket is present'
[ ! -e /var/lib/docker ] || fail 'the disposable job unexpectedly exposes host Docker data'

required_files=(
  host-release-seed.json host-image-bundle.tar software-release-seed.json
  software-image-bundle.tar host-release-cosign.pub software-release-cosign.pub
  cosign device-identity-cloud.pub license-trust.json public-config.json
  tailscale-client-lock.json registry-image.tar registry-config.yml registry.htpasswd
  registry-ca.crt registry.crt registry.key cloud-ca.crt cloud.crt cloud.key
  candidate-policy.json registry-credential.json device.json
)
for relative in "${required_files[@]}"; do
  [ -f "${FIXTURE_ROOT}/${relative}" ] && [ ! -L "${FIXTURE_ROOT}/${relative}" ] \
    || fail "fixture file missing: ${relative}"
done
for relative in identity-wheelhouse offline-apt registry-data; do
  [ -d "${FIXTURE_ROOT}/${relative}" ] && [ ! -L "${FIXTURE_ROOT}/${relative}" ] \
    || fail "fixture directory missing: ${relative}"
done
[ "$(stat -c '%a' "${FIXTURE_ROOT}/registry-credential.json")" = '400' ] \
  || fail 'registry credential fixture must be mode 0400'
[ "$(stat -c '%a' "${FIXTURE_ROOT}/registry.htpasswd")" = '400' ] \
  || fail 'registry password database fixture must be mode 0400'

for path in \
  /etc/drycontroller \
  /opt/Drycontroller-edge \
  /opt/drycontroller-host \
  /opt/drycontroller-identity \
  /opt/drycontroller-apt \
  /usr/libexec/drycontroller \
  /var/lib/drycontroller \
  /var/lib/drycontroller-host \
  /var/lib/drycontroller-ota; do
  [ ! -e "${path}" ] && [ ! -L "${path}" ] || fail "disposable image is not blank: ${path}"
done
[ ! -e /usr/local/bin/cosign ] || fail 'disposable image already has the installer cosign destination'

tmp_dir="$(mktemp -d /tmp/drycontroller-formal-e2e.XXXXXX)"
readonly tmp_dir
readonly daemon_data="${tmp_dir}/daemon-data"
readonly daemon_exec="${tmp_dir}/daemon-exec"
readonly daemon_pid="${tmp_dir}/dockerd.pid"
readonly daemon_log="${tmp_dir}/dockerd.log"
readonly daemon_config="${tmp_dir}/daemon.json"
readonly policy_log="${tmp_dir}/policy.log"
readonly writer_log="${tmp_dir}/writer.log"
readonly shims="${tmp_dir}/shims"
readonly attacker_bin="${tmp_dir}/attacker-bin"
readonly device_credential="${tmp_dir}/device-credential"
readonly operation_id_file="${tmp_dir}/operation-id"
readonly docker_config="${tmp_dir}/docker-config"
readonly fake_net="${tmp_dir}/sys-class-net"
readonly expected_host="${tmp_dir}/expected-host-release"
readonly audit_binding="${tmp_dir}/registry-audit-binding.json"
readonly audit_row="${tmp_dir}/registry-audit-row.json"

dockerd_started=0
dockerd_process_pid=''
registry_started=0
policy_pid=''
socket_probe_pid=''
plc_fixture_pid=''
tpm_created=0
net_mounted=0
cloud_address_added=0

docker_exact() {
  local config="${docker_config}"
  [ -d "${GUARD_CONFIG}" ] && config="${GUARD_CONFIG}"
  env -i HOME=/var/empty PATH=/usr/bin:/bin \
    DOCKER_HOST=unix:///run/docker.sock DOCKER_CONFIG="${config}" \
    "${DOCKER}" "$@"
}

run_plc_fixture_monitor() {
  local generation=''
  local state container_id container_pid running started_at
  while true; do
    state="$(docker_exact container inspect \
      --format '{{.Id}} {{.State.Pid}} {{.State.Running}} {{.State.StartedAt}}' \
      edge-daq 2>/dev/null || true)"
    if [ -n "${state}" ]; then
      read -r container_id container_pid running started_at <<<"${state}"
      if [ "${running}" = 'true' ] && [ "${container_id}:${started_at}" != "${generation}" ]; then
        if /usr/bin/nsenter --target "${container_pid}" --net \
          /usr/sbin/ip address replace "${PLC_FIXTURE_ADDRESS}/32" dev lo \
          && docker_exact container cp \
            "${SOURCE_ROOT}/scripts/ci/formal-modbus-fixture.py" \
            edge-daq:/tmp/formal-modbus-fixture.py \
          && docker_exact container exec --detach \
            --env "MODBUS_SIM_HOST=${PLC_FIXTURE_ADDRESS}" \
            --env MODBUS_SIM_PORT=2022 \
            edge-daq /usr/local/bin/python /tmp/formal-modbus-fixture.py; then
          generation="${container_id}:${started_at}"
        fi
      fi
    fi
    sleep 1
  done
}

stop_plc_fixture_monitor() {
  if [ -n "${plc_fixture_pid}" ]; then
    kill -TERM "${plc_fixture_pid}" >/dev/null 2>&1 || true
    wait "${plc_fixture_pid}" >/dev/null 2>&1 || true
    plc_fixture_pid=''
  fi
}

cleanup() {
  set +e
  stop_plc_fixture_monitor
  if [ -n "${policy_pid}" ]; then kill -TERM "${policy_pid}" >/dev/null 2>&1; fi
  if [ -n "${socket_probe_pid}" ]; then kill -TERM "${socket_probe_pid}" >/dev/null 2>&1; fi
  if [ -f "${tmp_dir}/writer.pid" ]; then
    kill -TERM "$(<"${tmp_dir}/writer.pid")" >/dev/null 2>&1
  fi
  if [ "${registry_started}" -eq 1 ]; then
    docker_exact container rm --force "${REGISTRY_CONTAINER}" >/dev/null 2>&1
  fi
  if [ "${dockerd_started}" -eq 1 ]; then
    if [ -f "${daemon_pid}" ]; then
      kill -TERM "$(<"${daemon_pid}")" >/dev/null 2>&1
    elif [ -n "${dockerd_process_pid}" ]; then
      kill -TERM "${dockerd_process_pid}" >/dev/null 2>&1
    fi
    if [ -n "${dockerd_process_pid}" ]; then
      wait "${dockerd_process_pid}" >/dev/null 2>&1 || true
    fi
  fi
  if [ "${net_mounted}" -eq 1 ]; then umount /sys/class/net >/dev/null 2>&1; fi
  if [ "${cloud_address_added}" -eq 1 ]; then
    ip address del "${CLOUD_ADDRESS}/32" dev lo >/dev/null 2>&1
  fi
  if [ "${tpm_created}" -eq 1 ]; then rm -f /dev/tpmrm0; fi
  rm -f /usr/local/share/ca-certificates/drycontroller-e2e-cloud.crt
  rm -rf \
    /etc/drycontroller \
    /etc/docker/certs.d/registry.e2e.test:5443 \
    /opt/Drycontroller-edge \
    /opt/drycontroller-host \
    /opt/drycontroller-identity \
    /opt/drycontroller-apt \
    /usr/libexec/drycontroller \
    /usr/local/bin/cosign \
    /var/lib/drycontroller \
    /var/lib/drycontroller-host \
    /var/lib/drycontroller-ota \
    /run/drycontroller-registry-auth
  rm -rf "${tmp_dir}"
}
trap cleanup EXIT

install -d -o root -g root -m 0700 \
  "${daemon_data}" "${daemon_exec}" "${docker_config}" "${shims}" "${attacker_bin}"
printf '%s' '{"auths":{}}' >"${docker_config}/config.json"
chmod 0600 "${docker_config}/config.json"
printf '%s' '{}' >"${daemon_config}"
chmod 0600 "${daemon_config}"
install -d -o root -g root -m 0755 /etc/docker/certs.d/registry.e2e.test:5443
install -o root -g root -m 0644 \
  "${FIXTURE_ROOT}/registry-ca.crt" \
  /etc/docker/certs.d/registry.e2e.test:5443/ca.crt
install -o root -g root -m 0644 \
  "${FIXTURE_ROOT}/cloud-ca.crt" \
  /usr/local/share/ca-certificates/drycontroller-e2e-cloud.crt
update-ca-certificates >/dev/null

"${DOCKERD}" \
  --config-file="${daemon_config}" \
  --host=unix:///run/docker.sock \
  --data-root="${daemon_data}" \
  --exec-root="${daemon_exec}" \
  --storage-driver=vfs \
  --pidfile="${daemon_pid}" \
  --bridge=none \
  --host-gateway-ip="${CLOUD_ADDRESS}" \
  >"${daemon_log}" 2>&1 &
dockerd_process_pid="$!"
dockerd_started=1
for _ in $(seq 1 300); do
  [ -S /run/docker.sock ] && docker_exact info >/dev/null 2>&1 && break
  kill -0 "${dockerd_process_pid}" >/dev/null 2>&1 \
    || fail 'disposable dockerd exited during startup'
  sleep 0.05
done
[ "$(docker_exact info --format '{{.DockerRootDir}}')" = "${daemon_data}" ] \
  || fail 'Docker CLI is not bound to the disposable data root'
[ "$(docker_exact info --format '{{.Driver}}')" = 'vfs' ] \
  || fail 'disposable dockerd is not using the nested-safe storage driver'
[ -z "$(docker_exact container ls --all --quiet --no-trunc)" ] \
  || fail 'disposable daemon is not container-empty'

docker_exact image load -i "${FIXTURE_ROOT}/registry-image.tar" >/dev/null
docker_exact image inspect "${REGISTRY_IMAGE}" >/dev/null \
  || fail 'fixture did not load the pinned registry image'
"${PYTHON}" "${SOURCE_ROOT}/scripts/prepare-host-release.py" stage \
  --source-root "${SOURCE_ROOT}" \
  --output-root "${expected_host}" \
  --topology-output "${tmp_dir}/expected-topology.json" >/dev/null
run_plc_fixture_monitor &
plc_fixture_pid="$!"

# The exact installer runs unchanged. Host service-manager/network/TPM and the
# external PLC are emulated by the disposable job; the PLC still uses the real
# RTU-over-TCP client. Release verification, fresh-install guard, image loading,
# Compose start and health are all real.
"${PYTHON}" - "${FIXTURE_ROOT}/tailscale-client-lock.json" >"${tmp_dir}/tailscale-version" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])
PY
readonly tailscale_version="$(<"${tmp_dir}/tailscale-version")"

install -m 0755 /dev/null "${shims}/systemctl"
"${PYTHON}" - "${shims}/systemctl" "${tmp_dir}" <<'PY'
import pathlib, sys
path, temporary = map(pathlib.Path, sys.argv[1:])
path.write_text(f'''#!/bin/sh
set -eu
case " $* " in
  *" drycontroller-registry-auth-writer.service "*)
    if [ ! -f "{temporary}/writer.pid" ]; then
      /usr/bin/python3 /usr/libexec/drycontroller/registry_auth_writer.py \\
        </dev/null >"{temporary}/writer.log" 2>&1 &
      printf '%s\\n' "$!" >"{temporary}/writer.pid"
    fi
    ;;
  *" drycontroller-factory-identity.service "*)
    install -d -o root -g root -m 0700 /var/lib/drycontroller
    install -o root -g root -m 0600 \\
      "$DRYCONTROLLER_E2E_FIXTURE/device.json" /var/lib/drycontroller/device.json
    ;;
esac
exit 0
''', encoding="utf-8")
path.chmod(0o755)
PY
install -m 0755 /dev/null "${shims}/curl"
printf '%s\n' '#!/bin/sh' "printf '%s\\n' '{\"status\":\"ok\"}'" >"${shims}/curl"
install -m 0755 /dev/null "${shims}/netplan"
printf '%s\n' '#!/bin/sh' 'exit 0' >"${shims}/netplan"
install -m 0755 /dev/null "${shims}/tailscale"
printf '%s\n' '#!/bin/sh' 'exit 0' >"${shims}/tailscale"
install -m 0755 /dev/null "${shims}/tpm2_createprimary"
printf '%s\n' '#!/bin/sh' 'exit 0' >"${shims}/tpm2_createprimary"
install -m 0755 /dev/null "${shims}/dpkg-query"
printf '%s\n' '#!/bin/sh' 'printf "%s" "$DRYCONTROLLER_E2E_TAILSCALE_VERSION"' >"${shims}/dpkg-query"

if [ ! -c /dev/tpmrm0 ]; then
  mknod /dev/tpmrm0 c 10 224
  chmod 0600 /dev/tpmrm0
  tpm_created=1
fi
if [ ! -e /sys/class/net/eth0/device ]; then
  install -d -m 0755 "${fake_net}/eth0/device" "${fake_net}/lo/device"
  printf '%s\n' '1' >"${fake_net}/eth0/type"
  printf '%s\n' '772' >"${fake_net}/lo/type"
  mount --bind "${fake_net}" /sys/class/net
  net_mounted=1
fi
cp -a "${FIXTURE_ROOT}/offline-apt" /opt/drycontroller-apt

DRYCONTROLLER_E2E_FIXTURE="${FIXTURE_ROOT}" \
DRYCONTROLLER_E2E_TAILSCALE_VERSION="${tailscale_version}" \
PATH="${shims}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
"${BASH}" "${SOURCE_ROOT}/infra/bootstrap/install-edge-host.sh" \
  --skip-packages \
  --source "${SOURCE_ROOT}" \
  --host-release "${FIXTURE_ROOT}/host-release-seed.json" \
  --host-image-bundle "${FIXTURE_ROOT}/host-image-bundle.tar" \
  --software-release "${FIXTURE_ROOT}/software-release-seed.json" \
  --software-image-bundle "${FIXTURE_ROOT}/software-image-bundle.tar" \
  --host-cosign-public-key "${FIXTURE_ROOT}/host-release-cosign.pub" \
  --software-cosign-public-key "${FIXTURE_ROOT}/software-release-cosign.pub" \
  --cosign-bin "${FIXTURE_ROOT}/cosign" \
  --device-identity-cloud-key "${FIXTURE_ROOT}/device-identity-cloud.pub" \
  --license-trust "${FIXTURE_ROOT}/license-trust.json" \
  --identity-wheelhouse "${FIXTURE_ROOT}/identity-wheelhouse" \
  --public-config "${FIXTURE_ROOT}/public-config.json" \
  --tailscale-client-lock "${FIXTURE_ROOT}/tailscale-client-lock.json"

for _ in $(seq 1 200); do
  [ -S /run/drycontroller-registry-auth/writer.sock ] && break
  sleep 0.05
done
[ -S /run/drycontroller-registry-auth/writer.sock ] \
  || fail 'formal service enable did not start the installed registry auth writer'
[ -f /var/lib/drycontroller-host/active.json ] \
  || fail 'formal initializer did not activate the signed initial pair'
[ -f /var/lib/drycontroller-ota/release-journal.json ] \
  || fail 'formal initializer did not create the software journal'
cmp -s "${expected_host}/docker-compose.yml" \
  /opt/drycontroller-host/current/docker-compose.yml \
  || fail 'signed initial pair did not carry the production HostRelease Compose'
[ ! -e "${AUTH_SOURCE}" ] \
  || fail 'installer created registry auth before credential refresh'

printf '%s\n' '127.0.0.1 registry.e2e.test' >>/etc/hosts
docker_exact run --detach --name "${REGISTRY_CONTAINER}" \
  --publish 127.0.0.1:5443:5443 \
  --volume "${FIXTURE_ROOT}/registry-config.yml:/etc/docker/registry/config.yml:ro" \
  --volume "${FIXTURE_ROOT}/registry.htpasswd:/auth/htpasswd:ro" \
  --volume "${FIXTURE_ROOT}/registry.crt:/certs/tls.crt:ro" \
  --volume "${FIXTURE_ROOT}/registry.key:/certs/tls.key:ro" \
  --volume "${FIXTURE_ROOT}/registry-data:/var/lib/registry:ro" \
  "${REGISTRY_IMAGE}" >/dev/null
registry_started=1
for _ in $(seq 1 200); do
  docker_exact container inspect --format '{{.State.Running}}' "${REGISTRY_CONTAINER}" \
    2>/dev/null | grep -qx true && break
  sleep 0.05
done
"${PYTHON}" - "${FIXTURE_ROOT}/registry-credential.json" <<'PY'
import base64
import json
import ssl
import sys
import time
import urllib.request
from pathlib import Path

credential = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
token = base64.b64encode(
    f"{credential['username']}:{credential['secret']}".encode("utf-8")
).decode("ascii")
request = urllib.request.Request(
    "https://registry.e2e.test:5443/v2/",
    headers={"Authorization": f"Basic {token}"},
)
context = ssl.create_default_context(cafile="/etc/docker/certs.d/registry.e2e.test:5443/ca.crt")
for _ in range(100):
    try:
        with urllib.request.urlopen(request, context=context, timeout=2) as response:
            if response.status == 200:
                break
    except OSError:
        time.sleep(0.05)
else:
    raise SystemExit("authenticated TLS registry did not become ready")
PY

# Exercise the live edge-api refresh route. The registry secret is read from a
# file by Python and never appears in argv, command output or service logs.
"${PYTHON}" - "${FIXTURE_ROOT}/registry-credential.json" <<'PY'
import json
import pathlib
import sys
import urllib.request

values = {}
for line in pathlib.Path("/etc/drycontroller/runtime.env").read_text(encoding="utf-8").splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        values[key] = value
admin = json.loads(values["ADMIN_API_KEYS"])[0]
payload = pathlib.Path(sys.argv[1]).read_bytes()
request = urllib.request.Request(
    "http://127.0.0.1:18080/api/v1/ops/registry-credentials/refresh",
    data=payload,
    headers={"Content-Type": "application/json", values.get("API_KEY_HEADER", "X-API-Key"): admin},
    method="POST",
)
with urllib.request.urlopen(request, timeout=15) as response:
    body = json.load(response)
if body.get("success") is not True or "secret" in json.dumps(body):
    raise SystemExit("live registry credential refresh response invalid")
PY
[ "$(stat -c '%u:%g:%a' "${AUTH_SOURCE}")" = '0:0:400' ] \
  || fail 'root registry auth was not atomically installed with fixed metadata'
[ ! -e /etc/drycontroller/registry/config.json ] \
  || fail 'legacy business-container registry secret cache still exists'
"${PYTHON}" - "${AUTH_SOURCE}" "${FIXTURE_ROOT}/registry-credential.json" "${audit_binding}" <<'PY'
import base64, json, pathlib, sys
document = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
requested = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
if set(document) != {"schema", "auths", "credential"}:
    raise SystemExit("root registry auth atomic document fields invalid")
if document["schema"] != "drycontroller.registry-auth.v2":
    raise SystemExit("root registry auth atomic document schema invalid")
status = document["credential"]
for key in ("credential_id", "registry_host", "username", "secret_fingerprint", "version"):
    expected = requested[key]
    if key == "registry_host":
        expected = expected.removeprefix("https://").removeprefix("http://").rstrip("/")
    if status.get(key) != expected:
        raise SystemExit(f"root registry auth status binding invalid: {key}")
if status.get("status") != "active" or not status.get("audit_id") or len(status.get("transaction_id", "")) != 32:
    raise SystemExit("root registry auth audit binding invalid")
encoded = document["auths"][status["registry_host"]]["auth"]
if base64.b64decode(encoded).decode("utf-8") != f"{requested['username']}:{requested['secret']}":
    raise SystemExit("root registry auth secret binding invalid")
pathlib.Path(sys.argv[3]).write_text(json.dumps({
    "audit_id": status["audit_id"],
    "transaction_id": status["transaction_id"],
    "version": status["version"],
    "secret_fingerprint": status["secret_fingerprint"],
}), encoding="utf-8")
PY
mapfile -t database_identity < <("${PYTHON}" - /etc/drycontroller/runtime.env <<'PY'
import pathlib, sys
values = {}
for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        values[key] = value
print(values["POSTGRES_USER"])
print(values["POSTGRES_DB"])
PY
)
readonly postgres_user="${database_identity[0]}"
readonly postgres_database="${database_identity[1]}"
readonly audit_id="$("${PYTHON}" - "${audit_binding}" <<'PY'
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["audit_id"]
if isinstance(value, bool) or not isinstance(value, int) or value < 1:
    raise SystemExit("registry audit ID invalid")
print(value)
PY
)"
docker_exact exec edge-postgres psql \
  --username "${postgres_user}" --dbname "${postgres_database}" \
  --tuples-only --no-align \
  --command "SELECT json_build_object('audit_id', id, 'transaction_id', correlation_id, 'result', result, 'version', detail->>'version', 'secret_fingerprint', detail->>'secret_fingerprint') FROM operation_audit_logs WHERE id = ${audit_id};" \
  >"${audit_row}"
"${PYTHON}" - "${audit_binding}" "${audit_row}" <<'PY'
import json, pathlib, sys
expected = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
actual = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
if actual != {
    "audit_id": expected["audit_id"],
    "transaction_id": expected["transaction_id"],
    "result": "accepted",
    "version": str(expected["version"]),
    "secret_fingerprint": expected["secret_fingerprint"],
}:
    raise SystemExit("root registry auth audit receipt does not match the committed audit row")
PY

"${PYTHON}" - \
  "${FIXTURE_ROOT}/candidate-policy.json" \
  "${FIXTURE_ROOT}/device.json" \
  "${operation_id_file}" \
  "${device_credential}" <<'PY'
import json, pathlib, sys
policy = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
identity = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
pathlib.Path(sys.argv[3]).write_text(policy["operation_id"], encoding="ascii")
pathlib.Path(sys.argv[4]).write_text(identity["runtime_credential"], encoding="utf-8")
pathlib.Path(sys.argv[4]).chmod(0o400)
PY
readonly operation_id="$(<"${operation_id_file}")"
ip address add "${CLOUD_ADDRESS}/32" dev lo
cloud_address_added=1
"${PYTHON}" "${SOURCE_ROOT}/scripts/ci/formal-ota-policy-server.py" \
  --policy "${FIXTURE_ROOT}/candidate-policy.json" \
  --credential "${device_credential}" \
  --certificate "${FIXTURE_ROOT}/cloud.crt" \
  --private-key "${FIXTURE_ROOT}/cloud.key" \
  --operation-id "${operation_id}" \
  --bind "${CLOUD_ADDRESS}" \
  --port "${CLOUD_PORT}" \
  >"${policy_log}" 2>&1 &
policy_pid="$!"
sleep 0.2
kill -0 "${policy_pid}" >/dev/null 2>&1 || fail 'TLS policy fixture did not start'
printf '{"operation_id":"%s"}' "${operation_id}" >"${STATE_ROOT}/request.json"
chmod 0600 "${STATE_ROOT}/request.json"

for name in docker bash docker-compose; do
  printf '%s\n' '#!/bin/sh' "printf invoked >'${tmp_dir}/attacker-${name}'" 'exit 91' \
    >"${attacker_bin}/${name}"
  chmod 0755 "${attacker_bin}/${name}"
done
DOCKER_HOST='tcp://127.0.0.1:9' \
DOCKER_CONTEXT='attacker' \
DOCKER_CONFIG="${tmp_dir}/attacker-config" \
DOCKER_CLI_PLUGIN_EXTRA_DIRS="${attacker_bin}" \
PATH="${attacker_bin}:/usr/local/bin:/usr/bin:/bin" \
"/opt/Drycontroller-edge/infra/ota-helper/ota-apply" >/dev/null
for name in docker bash docker-compose; do
  [ ! -e "${tmp_dir}/attacker-${name}" ] || fail "PATH ${name} replacement executed"
done

# Local reinspection/start remains independent of registry auth. Both absence
# and malformed contents are tested through the formal public entrypoint.
mv "${AUTH_SOURCE}" "${tmp_dir}/registry-auth.saved"
"/opt/Drycontroller-edge/infra/ota-helper/start-active-release" >/dev/null
printf '%s' 'not-json' >"${AUTH_SOURCE}"
chmod 0400 "${AUTH_SOURCE}"
"/opt/Drycontroller-edge/infra/ota-helper/start-active-release" >/dev/null
rm -f "${AUTH_SOURCE}"
mv "${tmp_dir}/registry-auth.saved" "${AUTH_SOURCE}"
stop_plc_fixture_monitor

expect_start_failure() {
  local context="$1"
  if "/opt/Drycontroller-edge/infra/ota-helper/start-active-release" \
    >"${tmp_dir}/negative.out" 2>&1; then
    fail "local start accepted ${context}"
  fi
}

cp -a "${DOCKER}" "${tmp_dir}/docker.saved"
install -o root -g root -m 0755 "${attacker_bin}/docker" "${DOCKER}"
expect_start_failure 'replaced Docker content'
install -o root -g root -m 0755 "${tmp_dir}/docker.saved" "${DOCKER}"

cp -a "${COMPOSE}" "${tmp_dir}/compose.saved"
install -o root -g root -m 0755 "${attacker_bin}/docker-compose" "${COMPOSE}"
expect_start_failure 'replaced Compose content'
install -o root -g root -m 0755 "${tmp_dir}/compose.saved" "${COMPOSE}"

chmod 0777 /usr/libexec/docker/cli-plugins
expect_start_failure 'writable Compose ancestor'
chmod 0755 /usr/libexec/docker/cli-plugins

cp -a /etc/drycontroller/docker-daemon.json "${tmp_dir}/daemon-identity.saved"
chmod 0600 /etc/drycontroller/docker-daemon.json
"${PYTHON}" - /etc/drycontroller/docker-daemon.json <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
value["id"] = "attacker-daemon"
path.write_text(json.dumps(value), encoding="utf-8")
path.chmod(0o400)
PY
expect_start_failure 'wrong daemon identity'
install -o root -g root -m 0400 "${tmp_dir}/daemon-identity.saved" \
  /etc/drycontroller/docker-daemon.json

# Replacing the fixed socket with another listening inode must fail before any
# Docker/Compose mutation. This socket belongs only to the self-dockerd job.
"${PYTHON}" - "${tmp_dir}/socket-probe.ready" "${tmp_dir}/socket-probe.go" <<'PY' &
import json, os, pathlib, subprocess, sys, time
ready, go = map(pathlib.Path, sys.argv[1:])
descriptor = os.open("/run/docker.sock", os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
try:
    ready.write_text("ready", encoding="ascii")
    for _ in range(500):
        if go.is_file():
            break
        time.sleep(0.02)
    else:
        raise SystemExit("descriptor-bound Docker probe was not released")
    completed = subprocess.run(
        [
            "/usr/bin/docker",
            "info",
            "--format",
            "{{.ID}}\\n{{.Name}}\\n{{.DockerRootDir}}",
        ],
        env={
            "HOME": "/var/empty",
            "PATH": "/usr/bin:/bin",
            "DOCKER_HOST": f"unix:///proc/self/fd/{descriptor}",
            "DOCKER_CONFIG": "/var/lib/drycontroller-ota/guard-docker-config",
        },
        pass_fds=(descriptor,),
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
finally:
    os.close(descriptor)
expected = json.loads(pathlib.Path("/etc/drycontroller/docker-daemon.json").read_text(encoding="utf-8"))
observed = dict(zip(("id", "name", "root_dir"), completed.stdout.splitlines(), strict=True))
if observed != {key: expected[key] for key in observed}:
    raise SystemExit("descriptor-bound Docker CLI reached the replacement path")
PY
socket_probe_pid="$!"
for _ in $(seq 1 100); do
  [ -f "${tmp_dir}/socket-probe.ready" ] && break
  sleep 0.02
done
[ -f "${tmp_dir}/socket-probe.ready" ] \
  || fail 'descriptor-bound Docker probe did not pin the original socket'
mv /run/docker.sock "${tmp_dir}/docker.sock.saved"
"${PYTHON}" - "${tmp_dir}/attacker-socket.pid" <<'PY' &
import os, pathlib, socket, sys, time
path = "/run/docker.sock"
server = socket.socket(socket.AF_UNIX)
server.bind(path)
os.chmod(path, 0o600)
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding="ascii")
server.listen()
time.sleep(30)
PY
for _ in $(seq 1 100); do
  [ -S /run/docker.sock ] && [ -f "${tmp_dir}/attacker-socket.pid" ] && break
  sleep 0.02
done
[ -S /run/docker.sock ] && [ -f "${tmp_dir}/attacker-socket.pid" ] \
  || fail 'replacement Docker socket fixture did not start'
touch "${tmp_dir}/socket-probe.go"
wait "${socket_probe_pid}" \
  || fail 'descriptor-bound Docker CLI did not remain on the original socket'
socket_probe_pid=''
expect_start_failure 'replacement Docker socket'
kill -TERM "$(<"${tmp_dir}/attacker-socket.pid")" >/dev/null 2>&1 || true
rm -f /run/docker.sock
mv "${tmp_dir}/docker.sock.saved" /run/docker.sock

"${PYTHON}" - \
  "${FIXTURE_ROOT}/registry-credential.json" \
  "${writer_log}" "${policy_log}" "${daemon_log}" <<'PY'
import json, pathlib, subprocess, sys
credential = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["secret"]
for value in sys.argv[2:]:
    path = pathlib.Path(value)
    if path.exists() and credential.encode() in path.read_bytes():
        raise SystemExit(f"registry secret leaked to {path.name}")
environment = {
    "HOME": "/var/empty",
    "PATH": "/usr/bin:/bin",
    "DOCKER_HOST": "unix:///run/docker.sock",
    "DOCKER_CONFIG": "/var/lib/drycontroller-ota/guard-docker-config",
}
containers = subprocess.run(
    ["/usr/bin/docker", "container", "ls", "--all", "--quiet", "--no-trunc"],
    env=environment,
    check=True,
    capture_output=True,
).stdout.splitlines()
for container in containers:
    logs = subprocess.run(
        ["/usr/bin/docker", "logs", "--tail", "1000", container.decode("ascii")],
        env=environment,
        check=False,
        capture_output=True,
    )
    if credential.encode() in logs.stdout or credential.encode() in logs.stderr:
        raise SystemExit("registry secret leaked to container logs")
PY

printf '%s\n' \
  'formal-install private OTA E2E passed: self-dockerd=1 installer=exact initializer=signed-pair refresh=live-edge-api audit=bound private-pull=authenticated-tls ota-apply=public-entrypoint local-auth=missing,malformed legacy-cache=absent path-context=refused tool-content=refused ancestor=refused daemon=refused socket-fd=pinned socket=refused'
