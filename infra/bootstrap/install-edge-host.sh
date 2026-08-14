#!/usr/bin/env bash
# Install the public-material-only RB-6 runtime and one signed H1/T1/S1 pair.
set -euo pipefail

INSTALL_PACKAGES=true
REPO_SOURCE=""
HOST_RELEASE=""
HOST_IMAGE_BUNDLE=""
SOFTWARE_RELEASE=""
SOFTWARE_IMAGE_BUNDLE=""
HOST_COSIGN_PUBLIC_KEY=""
SOFTWARE_COSIGN_PUBLIC_KEY=""
COSIGN_BIN=""
DEVICE_IDENTITY_CLOUD_PUBLIC_KEY=""
IDENTITY_WHEELHOUSE=""
PUBLIC_CONFIG=""
COSIGN_APPROVED_VERSION="v3.1.3"
COSIGN_APPROVED_LINUX_AMD64_SHA256="4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71"

usage() {
  cat <<'EOF'
Usage:
  sudo bash infra/bootstrap/install-edge-host.sh [options]

Required public release inputs:
  --host-release FILE                 Completed signed HostRelease H1 seed
  --host-image-bundle FILE            Offline OCI archive for H1
  --software-release FILE             Completed signed SoftwareRelease S1 seed
  --software-image-bundle FILE        Complete offline image archive for S1
  --host-cosign-public-key FILE       HostRelease verification key
  --software-cosign-public-key FILE   SoftwareRelease verification key
  --cosign-bin FILE                   Approved Linux amd64 cosign binary
  --device-identity-cloud-key FILE    Cloud activation receipt verification key
  --identity-wheelhouse DIR           Offline Python wheels for device identity
  --public-config FILE                HTTPS Cloud URL and one fixed factory NIC

Other:
  --source PATH                       Source package containing the installer
  --skip-packages                     Verify installed packages without apt-get
  -h, --help                          Show this help

The installer accepts no batch session, bootstrap token, Tailnet key, registry
credential, production API key, private signing key, or file-key fallback.
EOF
}

log() {
  printf '[%s] %s\n' "$(date -Is)" "$*"
}

die() {
  log "ERROR: $*" >&2
  exit 1
}

required_value() {
  [ -n "${2:-}" ] || die "$1 requires a value"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --source) required_value "$1" "${2:-}"; REPO_SOURCE="$2"; shift 2 ;;
    --skip-packages) INSTALL_PACKAGES=false; shift ;;
    --host-release) required_value "$1" "${2:-}"; HOST_RELEASE="$2"; shift 2 ;;
    --host-image-bundle) required_value "$1" "${2:-}"; HOST_IMAGE_BUNDLE="$2"; shift 2 ;;
    --software-release) required_value "$1" "${2:-}"; SOFTWARE_RELEASE="$2"; shift 2 ;;
    --software-image-bundle) required_value "$1" "${2:-}"; SOFTWARE_IMAGE_BUNDLE="$2"; shift 2 ;;
    --host-cosign-public-key) required_value "$1" "${2:-}"; HOST_COSIGN_PUBLIC_KEY="$2"; shift 2 ;;
    --software-cosign-public-key) required_value "$1" "${2:-}"; SOFTWARE_COSIGN_PUBLIC_KEY="$2"; shift 2 ;;
    --cosign-bin) required_value "$1" "${2:-}"; COSIGN_BIN="$2"; shift 2 ;;
    --device-identity-cloud-key) required_value "$1" "${2:-}"; DEVICE_IDENTITY_CLOUD_PUBLIC_KEY="$2"; shift 2 ;;
    --identity-wheelhouse) required_value "$1" "${2:-}"; IDENTITY_WHEELHOUSE="$2"; shift 2 ;;
    --public-config) required_value "$1" "${2:-}"; PUBLIC_CONFIG="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "installer must run as root"
[ "$(uname -s)" = "Linux" ] || die "Linux is required"
[ "$(uname -m)" = "x86_64" ] || die "Linux amd64 is required"
[ -c /dev/tpmrm0 ] || die "TPM 2.0 resource manager /dev/tpmrm0 is required"
command -v systemctl >/dev/null 2>&1 || die "systemd is required"

if [ -z "$REPO_SOURCE" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  REPO_SOURCE="$(cd "${SCRIPT_DIR}/../.." && pwd)"
else
  REPO_SOURCE="$(cd "$REPO_SOURCE" && pwd)"
fi

for path in \
  "$HOST_RELEASE" \
  "$HOST_IMAGE_BUNDLE" \
  "$SOFTWARE_RELEASE" \
  "$SOFTWARE_IMAGE_BUNDLE" \
  "$HOST_COSIGN_PUBLIC_KEY" \
  "$SOFTWARE_COSIGN_PUBLIC_KEY" \
  "$COSIGN_BIN" \
  "$DEVICE_IDENTITY_CLOUD_PUBLIC_KEY" \
  "$PUBLIC_CONFIG"; do
  [ -f "$path" ] || die "required release input missing: $path"
done
[ -d "$IDENTITY_WHEELHOUSE" ] || die "device identity wheelhouse missing"
[ -x "$COSIGN_BIN" ] || die "cosign input must be executable"

for path in \
  env.example \
  infra/bootstrap/initialize-runtime-config.py \
  infra/bootstrap/installer_public_config.py \
  infra/host-release-runner/host_release_engine.py \
  infra/host-release-runner/host_release_runner.py \
  infra/host-release-runner/host-release-runner \
  infra/host-release-runner/host-release-apply \
  infra/host-release-runner/initialize-initial-release \
  infra/host-release-runner/drycontroller-host-release-runner.service \
  infra/ota-helper/drycontroller-ota-helper.service \
  infra/bootstrap/drycontroller-factory-identity.service \
  infra/bootstrap/drycontroller-device-credential-rotation.service \
  infra/bootstrap/drycontroller-device-credential-rotation.timer \
  services/device-identity/requirements.txt; do
  [ -f "${REPO_SOURCE}/${path}" ] || die "installer source missing: $path"
done
[ -d "${REPO_SOURCE}/services/device-identity/identity_service" ] \
  || die "device identity source missing"

command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
actual_cosign_sha256="$(sha256sum "$COSIGN_BIN" | awk '{print $1}')"
[ "$actual_cosign_sha256" = "$COSIGN_APPROVED_LINUX_AMD64_SHA256" ] \
  || die "Linux amd64 cosign binary checksum is not approved"
cosign_version_output="$("$COSIGN_BIN" version 2>/dev/null)" \
  || die "Linux amd64 cosign binary cannot report its version"
grep -Eq "GitVersion:[[:space:]]+${COSIGN_APPROVED_VERSION//./\\.}([[:space:]]|$)" <<<"$cosign_version_output" \
  || die "Linux amd64 cosign version is not approved"
grep -Eq 'Platform:[[:space:]]+linux/amd64([[:space:]]|$)' <<<"$cosign_version_output" \
  || die "cosign binary platform is not linux/amd64"

python3 - "$DEVICE_IDENTITY_CLOUD_PUBLIC_KEY" <<'PY'
import base64
import binascii
import sys
from pathlib import Path

try:
    raw = base64.b64decode(Path(sys.argv[1]).read_text(encoding="ascii").strip(), validate=True)
except (OSError, UnicodeDecodeError, binascii.Error, ValueError) as exc:
    raise SystemExit("Cloud identity public key is invalid") from exc
if len(raw) != 32:
    raise SystemExit("Cloud identity public key must be Ed25519 raw public bytes")
PY

install_packages() {
  if [ "$INSTALL_PACKAGES" = true ]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends \
      ca-certificates curl docker.io docker-compose-v2 iproute2 netplan.io \
      python3 python3-venv tar tpm2-tools
  fi
  command -v docker >/dev/null 2>&1 || die "docker is not installed"
  docker compose version >/dev/null 2>&1 || die "docker compose v2 is not installed"
  command -v netplan >/dev/null 2>&1 || die "netplan is not installed"
  command -v tpm2_createprimary >/dev/null 2>&1 || die "tpm2-tools are not installed"
  python3 -m venv --help >/dev/null 2>&1 || die "python3 venv support is not installed"
}

configure_docker() {
  install -d -m 0755 /etc/docker
  if [ ! -e /etc/docker/daemon.json ]; then
    install -m 0600 /dev/null /etc/docker/daemon.json
    printf '%s\n' '{"log-driver":"json-file","log-opts":{"max-size":"100m","max-file":"5"},"features":{"buildkit":false}}' \
      > /etc/docker/daemon.json
  fi
  systemctl enable docker.service >/dev/null
  systemctl restart docker.service
}

install_static_runtime() {
  install -d -m 0755 /usr/libexec/drycontroller /etc/drycontroller/keys /etc/drycontroller/trust
  install -d -m 0700 /var/lib/drycontroller /var/lib/drycontroller-host /var/lib/drycontroller-ota
  install -o root -g root -m 0644 "${REPO_SOURCE}/infra/host-release-runner/host_release_engine.py" /usr/libexec/drycontroller/host_release_engine.py
  install -o root -g root -m 0644 "${REPO_SOURCE}/infra/host-release-runner/host_release_runner.py" /usr/libexec/drycontroller/host_release_runner.py
  install -o root -g root -m 0755 "${REPO_SOURCE}/infra/host-release-runner/host-release-runner" /usr/libexec/drycontroller/host-release-runner
  install -o root -g root -m 0755 "${REPO_SOURCE}/infra/host-release-runner/host-release-apply" /usr/libexec/drycontroller/host-release-apply
  install -o root -g root -m 0755 "${REPO_SOURCE}/infra/host-release-runner/initialize-initial-release" /usr/libexec/drycontroller/initialize-initial-release
  install -o root -g root -m 0644 "${REPO_SOURCE}/infra/host-release-runner/drycontroller-host-release-runner.service" /etc/systemd/system/drycontroller-host-release-runner.service
  install -o root -g root -m 0644 "${REPO_SOURCE}/infra/ota-helper/drycontroller-ota-helper.service" /etc/systemd/system/drycontroller-ota-helper.service
  install -o root -g root -m 0644 "$HOST_COSIGN_PUBLIC_KEY" /etc/drycontroller/keys/host-release-cosign.pub
  install -o root -g root -m 0644 "$SOFTWARE_COSIGN_PUBLIC_KEY" /etc/drycontroller/keys/cosign.pub
  install -o root -g root -m 0644 "$DEVICE_IDENTITY_CLOUD_PUBLIC_KEY" /etc/drycontroller/trust/device-identity-cloud.pub
  install -o root -g root -m 0755 "$COSIGN_BIN" /usr/local/bin/cosign
  [ "$(sha256sum /usr/local/bin/cosign | awk '{print $1}')" = "$COSIGN_APPROVED_LINUX_AMD64_SHA256" ] \
    || die "installed cosign checksum changed"
}

install_identity_service() {
  install -d -m 0755 /opt/drycontroller-identity
  rm -rf /opt/drycontroller-identity/identity_service /opt/drycontroller-identity/venv
  cp -a "${REPO_SOURCE}/services/device-identity/identity_service" /opt/drycontroller-identity/identity_service
  find /opt/drycontroller-identity/identity_service -type d -exec chmod 0755 {} +
  find /opt/drycontroller-identity/identity_service -type f -exec chmod 0644 {} +
  install -m 0644 "${REPO_SOURCE}/services/device-identity/requirements.txt" /opt/drycontroller-identity/requirements.txt
  python3 -m venv /opt/drycontroller-identity/venv
  /opt/drycontroller-identity/venv/bin/pip install \
    --disable-pip-version-check --no-index --find-links "$IDENTITY_WHEELHOUSE" \
    -r /opt/drycontroller-identity/requirements.txt
  /opt/drycontroller-identity/venv/bin/python -m pip check
  install -m 0644 "${REPO_SOURCE}/infra/bootstrap/drycontroller-factory-identity.service" /etc/systemd/system/drycontroller-factory-identity.service
  install -m 0644 "${REPO_SOURCE}/infra/bootstrap/drycontroller-device-credential-rotation.service" /etc/systemd/system/drycontroller-device-credential-rotation.service
  install -m 0644 "${REPO_SOURCE}/infra/bootstrap/drycontroller-device-credential-rotation.timer" /etc/systemd/system/drycontroller-device-credential-rotation.timer
}

initialize_configuration() {
  python3 "${REPO_SOURCE}/infra/bootstrap/initialize-runtime-config.py" \
    --public-config "$PUBLIC_CONFIG" \
    --env-template "${REPO_SOURCE}/env.example"
  netplan generate
  netplan apply
}

initialize_release() {
  if [ -e /opt/Drycontroller-edge ] && [ ! -L /opt/Drycontroller-edge ]; then
    die "/opt/Drycontroller-edge must not contain a legacy checkout"
  fi
  if [ ! -L /opt/Drycontroller-edge ]; then
    ln -s /opt/drycontroller-host/current /opt/Drycontroller-edge
  fi
  [ "$(readlink /opt/Drycontroller-edge)" = "/opt/drycontroller-host/current" ] \
    || die "/opt/Drycontroller-edge compatibility link is not immutable"
  /usr/libexec/drycontroller/initialize-initial-release \
    "$HOST_RELEASE" "$SOFTWARE_RELEASE" "$HOST_IMAGE_BUNDLE" "$SOFTWARE_IMAGE_BUNDLE"
}

enable_services() {
  systemctl daemon-reload
  systemctl enable --now drycontroller-factory-identity.service
  identity_healthy=false
  for _ in $(seq 1 30); do
    if curl --fail --silent --show-error --max-time 2 http://169.254.203.2:8765/health \
      | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("status") == "ok" else 1)'; then
      identity_healthy=true
      break
    fi
    sleep 1
  done
  [ "$identity_healthy" = true ] && systemctl is-active --quiet drycontroller-factory-identity.service \
    || die "TPM factory identity service did not become healthy"
  systemctl enable --now drycontroller-device-credential-rotation.timer
  systemctl enable --now drycontroller-ota-helper.service
  systemctl enable --now drycontroller-host-release-runner.service
}

write_host_manifest() {
  python3 - /var/lib/drycontroller/host-install.json "$HOST_RELEASE" "$SOFTWARE_RELEASE" <<'PY'
import datetime as dt
import json
import os
import sys
from pathlib import Path

target, host_path, software_path = map(Path, sys.argv[1:])
host = json.loads(host_path.read_text(encoding="utf-8"))["manifest"]
software = json.loads(software_path.read_text(encoding="utf-8"))["manifest"]
payload = {
    "schema": "drycontroller.host-install.v2",
    "installed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    "host_release_id": host["release_id"],
    "host_sequence": host["sequence"],
    "topology_digest": host["topology_digest"],
    "software_release_id": software["release_id"],
    "software_sequence": software["sequence"],
}
temporary = target.with_name(".host-install.json.tmp")
with temporary.open("w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, target)
os.chmod(target, 0o600)
directory = os.open(target.parent, os.O_RDONLY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
}

log "preparing one blank DryController Edge host"
install_packages
configure_docker
install_static_runtime
install_identity_service
initialize_configuration
initialize_release
enable_services
write_host_manifest
log "public-material installation complete; active H1/T1/S1 and TPM identity are healthy"
