#!/usr/bin/env bash
#
# Prepare a plain Linux host for the signed DryController runtime.
#
# This script installs the host-level prerequisites and places the edge project
# plus the root-owned OTA helper. Per-device identity is installed only by the
# later RB-6 media after the RB-5B TPM backend is complete.
#
set -euo pipefail

EDGE_ROOT="/opt/Drycontroller-edge"
INSTALL_PACKAGES=true
INSTALL_DOCKER=true
INSTALL_TAILSCALE=true
REPO_SOURCE=""
INITIAL_RELEASE=""
INITIAL_IMAGE_BUNDLE=""
COSIGN_PUBLIC_KEY=""
COSIGN_BIN=""
RELEASE_LOCK="/var/lib/drycontroller-ota/release.lock"
HOST_ACTIVE_SOURCE=""
HOST_ACTIVE_TARGET="/var/lib/drycontroller-host/active.json"
COSIGN_APPROVED_VERSION="v3.1.3"
COSIGN_APPROVED_LINUX_AMD64_SHA256="4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71"

usage() {
  cat <<'EOF'
Usage:
  sudo bash infra/bootstrap/install-edge-host.sh [options]

Options:
  --source PATH          Source repo/package directory. Default: repository root containing this script
  --skip-packages        Do not run apt-get; only verify prerequisites
  --skip-docker          Do not install Docker; only verify it exists
  --skip-tailscale       Do not install Tailscale; only verify it exists
  --initial-release FILE Completed signed SoftwareRelease seed JSON
  --image-bundle FILE    Complete digest-locked Docker image archive for the seed
  --cosign-public-key FILE  Public key bound by the SoftwareRelease manifest
  --cosign-bin FILE      Linux amd64 cosign binary for offline verification
  --host-active FILE     Fixed root-owned H1/T1 state paired with the SoftwareRelease
  -h, --help             Show this help

Expected follow-up:
  Use the future RB-6 media to install the RB-5B TPM identity backend. This
  RB-5A branch is deliberately not a deployable production installer.
EOF
}

log() {
  printf '[%s] %s\n' "$(date -Is)" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --source)
      REPO_SOURCE="${2:-}"
      [ -n "$REPO_SOURCE" ] || die "--source requires a value"
      shift 2
      ;;
    --skip-packages)
      INSTALL_PACKAGES=false
      INSTALL_DOCKER=false
      INSTALL_TAILSCALE=false
      shift
      ;;
    --skip-docker)
      INSTALL_DOCKER=false
      shift
      ;;
    --skip-tailscale)
      INSTALL_TAILSCALE=false
      shift
      ;;
    --initial-release)
      INITIAL_RELEASE="${2:-}"
      [ -n "$INITIAL_RELEASE" ] || die "--initial-release requires a value"
      shift 2
      ;;
    --image-bundle)
      INITIAL_IMAGE_BUNDLE="${2:-}"
      [ -n "$INITIAL_IMAGE_BUNDLE" ] || die "--image-bundle requires a value"
      shift 2
      ;;
    --cosign-public-key)
      COSIGN_PUBLIC_KEY="${2:-}"
      [ -n "$COSIGN_PUBLIC_KEY" ] || die "--cosign-public-key requires a value"
      shift 2
      ;;
    --cosign-bin)
      COSIGN_BIN="${2:-}"
      [ -n "$COSIGN_BIN" ] || die "--cosign-bin requires a value"
      shift 2
      ;;
    --host-active)
      HOST_ACTIVE_SOURCE="${2:-}"
      [ -n "$HOST_ACTIVE_SOURCE" ] || die "--host-active requires a value"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown argument: $1"
      ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root, for example: sudo bash infra/bootstrap/install-edge-host.sh"
[ "$(uname -s)" = "Linux" ] || die "Linux is required"
[ "$(uname -m)" = "x86_64" ] || die "Linux amd64 is required"
command -v systemctl >/dev/null 2>&1 || die "systemd is required"
[ -f "$INITIAL_RELEASE" ] || die "signed initial SoftwareRelease seed is required"
[ -f "$INITIAL_IMAGE_BUNDLE" ] || die "complete initial release image bundle is required"
[ -f "$COSIGN_PUBLIC_KEY" ] || die "initial release cosign public key is required"
[ -x "$COSIGN_BIN" ] || die "executable Linux amd64 cosign binary is required"
[ -f "$HOST_ACTIVE_SOURCE" ] || die "fixed active HostRelease state is required"

if [ -z "$REPO_SOURCE" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  REPO_SOURCE="$(cd "${SCRIPT_DIR}/../.." && pwd)"
else
  REPO_SOURCE="$(cd "$REPO_SOURCE" && pwd)"
fi

[ -f "${REPO_SOURCE}/docker-compose.yml" ] || die "docker-compose.yml not found in source: ${REPO_SOURCE}"
[ -f "${REPO_SOURCE}/env.example" ] || die "env.example not found in source: ${REPO_SOURCE}"
[ -f "${REPO_SOURCE}/infra/ota-helper/drycontroller_ota_helper.py" ] || die "OTA helper missing in source"
[ -f "${REPO_SOURCE}/infra/ota-helper/release_engine.py" ] || die "OTA release engine missing in source"
[ -f "${REPO_SOURCE}/infra/ota-helper/ota-apply" ] || die "fixed OTA apply program missing in source"
[ -f "${REPO_SOURCE}/infra/ota-helper/initialize-active-release" ] || die "active release initializer missing in source"
[ -f "${REPO_SOURCE}/infra/ota-helper/start-active-release" ] || die "active release starter missing in source"
[ -f "${REPO_SOURCE}/infra/bootstrap/release-transaction-lock.sh" ] || die "factory release lock helper missing in source"

command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
# shellcheck source=infra/bootstrap/release-transaction-lock.sh
source "${REPO_SOURCE}/infra/bootstrap/release-transaction-lock.sh"
factory_release_lock_acquire "$RELEASE_LOCK" || die "another release or factory install transaction is active"
[ ! -e /var/lib/drycontroller-ota/release-journal.json ] || die "factory installer refuses an existing release journal"
[ ! -e "$HOST_ACTIVE_TARGET" ] || die "factory installer refuses existing active HostRelease state"

actual_cosign_sha256="$(sha256sum "$COSIGN_BIN" | awk '{print $1}')"
[ "$actual_cosign_sha256" = "$COSIGN_APPROVED_LINUX_AMD64_SHA256" ] || die "Linux amd64 cosign binary checksum is not approved"
cosign_version_output="$("$COSIGN_BIN" version 2>/dev/null)" || die "Linux amd64 cosign binary cannot report its version"
grep -Eq "GitVersion:[[:space:]]+${COSIGN_APPROVED_VERSION//./\\.}([[:space:]]|$)" <<<"$cosign_version_output" \
  || die "Linux amd64 cosign version is not approved"
grep -Eq 'Platform:[[:space:]]+linux/amd64([[:space:]]|$)' <<<"$cosign_version_output" \
  || die "cosign binary platform is not linux/amd64"
PYTHONPATH="${REPO_SOURCE}/infra/ota-helper" python3 - \
  "$INITIAL_RELEASE" "$COSIGN_APPROVED_VERSION" "sha256:${COSIGN_APPROVED_LINUX_AMD64_SHA256}" \
  "$HOST_ACTIVE_SOURCE" "${REPO_SOURCE}/docker-compose.yml" <<'PY'
import json
import sys
from pathlib import Path

from release_engine import (
    assert_software_release_host_binding,
    canonical_manifest_digest,
    validate_host_active,
    validate_manifest,
)

seed = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(seed, dict) or set(seed) != {"manifest", "manifest_digest", "manifest_signature"}:
    raise SystemExit("initial release seed fields invalid")
manifest = validate_manifest(seed["manifest"])
if seed["manifest_digest"] != canonical_manifest_digest(manifest):
    raise SystemExit("initial release seed digest mismatch")
if manifest["cosign"] != {"version": sys.argv[2], "linux_amd64_sha256": sys.argv[3]}:
    raise SystemExit("initial release cosign toolchain is not approved")
host_active = validate_host_active(
    json.loads(Path(sys.argv[4]).read_text(encoding="utf-8")),
    Path(sys.argv[5]).read_bytes(),
)
assert_software_release_host_binding(manifest, host_active)
PY

install_base_packages_apt() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    python3 \
    tar \
    iproute2 \
    lsb-release
}

install_docker_apt() {
  if command -v docker >/dev/null 2>&1; then
    log "docker already installed: $(docker --version)"
  else
    log "installing docker from OS package repository"
    apt-get install -y --no-install-recommends docker.io
  fi

  if docker compose version >/dev/null 2>&1; then
    log "docker compose plugin available: $(docker compose version)"
  else
    log "installing docker compose plugin from OS package repository"
    if apt-cache show docker-compose-v2 >/dev/null 2>&1; then
      apt-get install -y --no-install-recommends docker-compose-v2
    elif apt-cache show docker-compose-plugin >/dev/null 2>&1; then
      apt-get install -y --no-install-recommends docker-compose-plugin
    elif command -v docker-compose >/dev/null 2>&1; then
      log "docker compose v2 unavailable; legacy docker-compose present: $(docker-compose --version)"
    else
      apt-get install -y --no-install-recommends docker-compose
    fi
  fi
}

install_tailscale_apt() {
  if command -v tailscale >/dev/null 2>&1; then
    log "tailscale already installed: $(tailscale version 2>/dev/null | head -n 1 || true)"
    return
  fi

  if apt-cache show tailscale >/dev/null 2>&1; then
    log "installing tailscale from configured apt repository"
    apt-get install -y --no-install-recommends tailscale
    return
  fi

  # Mainland China: pkgs.tailscale.com is not a dependable field-time dependency.
  # Factory media must either preinstall tailscale or include it in the offline apt repo.
  die "tailscale is not installed; include tailscale in the OS image/offline apt repo"
}

install_host_packages() {
  if [ "$INSTALL_PACKAGES" != true ]; then
    log "package installation skipped"
    return
  fi

  if command -v apt-get >/dev/null 2>&1; then
    install_base_packages_apt
    if [ "$INSTALL_DOCKER" = true ]; then
      install_docker_apt
    fi
    if [ "$INSTALL_TAILSCALE" = true ]; then
      install_tailscale_apt
    fi
    return
  fi

  log "apt-get not found; skipping package installation"
  log "supported manual prerequisites: docker, docker compose, python3, curl, tar"
}

verify_prerequisites() {
  command -v docker >/dev/null 2>&1 || die "docker is not installed"
  if ! docker compose version >/dev/null 2>&1; then
    die "docker compose v2 plugin is not installed"
  fi
  command -v python3 >/dev/null 2>&1 || die "python3 is not installed"
  command -v curl >/dev/null 2>&1 || die "curl is not installed"
  command -v tailscale >/dev/null 2>&1 || die "tailscale is not installed"
}

configure_docker() {
  mkdir -p /etc/docker
  if [ ! -f /etc/docker/daemon.json ]; then
    cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "100m",
    "max-file": "5"
  },
  "registry-mirrors": ["https://docker.m.daocloud.io"],
  "dns": ["223.5.5.5", "114.114.114.114"],
  "features": {"buildkit": false}
}
EOF
    log "created /etc/docker/daemon.json with log rotation, registry mirror, dns, buildkit disabled"
  else
    log "/etc/docker/daemon.json exists; leaving it unchanged"
  fi

  systemctl enable docker.service >/dev/null 2>&1 || true
  systemctl restart docker.service
}

copy_edge_project() {
  log "installing edge project: ${REPO_SOURCE} -> ${EDGE_ROOT}"
  mkdir -p "$(dirname "$EDGE_ROOT")"
  if [ -d "$EDGE_ROOT" ] && [ -f "${EDGE_ROOT}/docker-compose.yml" ]; then
    BACKUP="${EDGE_ROOT}.bak.$(date +%Y%m%d%H%M%S)"
    log "existing edge root found; moving to ${BACKUP}"
    mv "$EDGE_ROOT" "$BACKUP"
  fi
  mkdir -p "$EDGE_ROOT"
  (
    cd "$REPO_SOURCE"
    tar \
      --exclude='.git' \
      --exclude='.env' \
      --exclude='.env.*' \
      --exclude='.venv' \
      --exclude='.venv-*' \
      --exclude='node_modules' \
      --exclude='__pycache__' \
      --exclude='.pytest_cache' \
      --exclude='*.pyc' \
      -cf - .
  ) | (
    cd "$EDGE_ROOT"
    tar --no-same-owner -xf -
  )

  if [ ! -f "${EDGE_ROOT}/.env" ]; then
    cp "${EDGE_ROOT}/env.example" "${EDGE_ROOT}/.env"
    chmod 0600 "${EDGE_ROOT}/.env"
    log "created ${EDGE_ROOT}/.env from env.example"
  fi
}

install_host_services() {
  retire_legacy_bootstrap_service
  systemctl disable --now drycontroller-ota-helper.service >/dev/null 2>&1 || true

  mkdir -p /etc/drycontroller/keys /etc/drycontroller/license /var/lib/drycontroller /var/log
  chmod 755 /etc/drycontroller /etc/drycontroller/keys
  chmod 750 /etc/drycontroller/license
  chmod 700 /var/lib/drycontroller
  install -d -m 0755 /usr/libexec/drycontroller
  install -o root -g root -m 0755 "${EDGE_ROOT}/infra/ota-helper/drycontroller_ota_helper.py" /usr/libexec/drycontroller/drycontroller_ota_helper.py
  install -o root -g root -m 0644 "${EDGE_ROOT}/infra/ota-helper/release_engine.py" /usr/libexec/drycontroller/release_engine.py
  install -o root -g root -m 0755 "${EDGE_ROOT}/infra/ota-helper/ota-apply" /usr/libexec/drycontroller/ota-apply
  install -o root -g root -m 0755 "${EDGE_ROOT}/infra/ota-helper/initialize-active-release" /usr/libexec/drycontroller/initialize-active-release
  install -o root -g root -m 0755 "${EDGE_ROOT}/infra/ota-helper/start-active-release" /usr/libexec/drycontroller/start-active-release
  install -o root -g root -m 0644 "${EDGE_ROOT}/infra/ota-helper/drycontroller-ota-helper.service" /etc/systemd/system/drycontroller-ota-helper.service
  install -o root -g root -m 0644 "$COSIGN_PUBLIC_KEY" /etc/drycontroller/keys/cosign.pub
  install -o root -g root -m 0755 "$COSIGN_BIN" /usr/local/bin/cosign

  installed_cosign_sha256="$(sha256sum /usr/local/bin/cosign | awk '{print $1}')"
  [ "$installed_cosign_sha256" = "$COSIGN_APPROVED_LINUX_AMD64_SHA256" ] \
    || die "installed cosign checksum changed"
  /usr/local/bin/cosign version >/dev/null
  log "loading complete initial release image bundle"
  docker image load -i "$INITIAL_IMAGE_BUNDLE"
  install -d -o root -g root -m 0700 "$(dirname "$HOST_ACTIVE_TARGET")"
  install -o root -g root -m 0600 "$HOST_ACTIVE_SOURCE" "$HOST_ACTIVE_TARGET"
  log "verifying and initializing signed active release"
  if ! /usr/libexec/drycontroller/initialize-active-release --lock-fd 9 "$INITIAL_RELEASE"; then
    rm -f "$HOST_ACTIVE_TARGET"
    die "signed active release initialization failed"
  fi

  systemctl daemon-reload
  systemctl enable --now drycontroller-ota-helper.service
  log "initialized active release and enabled the root-owned OTA helper"
}

retire_legacy_bootstrap_service() {
  systemctl disable --now drycontroller-bootstrap.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/drycontroller-bootstrap.service
  rm -f /usr/local/sbin/drycontroller-bootstrap.sh
  rm -rf /etc/systemd/system/drycontroller-bootstrap.service.d
  log "removed retired drycontroller-bootstrap.service if present"
}

write_host_manifest() {
  mkdir -p /var/lib/drycontroller
  cat > /var/lib/drycontroller/host-install.json <<EOF
{
  "installed_at": "$(date -Is)",
  "edge_root": "${EDGE_ROOT}",
  "source": "${REPO_SOURCE}",
  "ota_helper_service": "drycontroller-ota-helper.service",
  "docker": "$(docker --version 2>/dev/null || true)",
  "compose": "$(docker compose version 2>/dev/null || docker-compose --version 2>/dev/null || true)",
  "tailscale": "$(tailscale version 2>/dev/null | head -n 1 || true)"
}
EOF
  chmod 0600 /var/lib/drycontroller/host-install.json
}

log "preparing Drycontroller edge host"
install_host_packages
verify_prerequisites
configure_docker
copy_edge_project
install_host_services
write_host_manifest

log "host preparation complete"
log "production identity installation remains blocked until RB-5B and RB-6"
