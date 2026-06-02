#!/usr/bin/env bash
#
# Prepare a plain Linux host for Drycontroller edge factory activation.
#
# This script installs the host-level prerequisites and places the edge project
# plus the host tailnet / activation services where non-developer installers
# expect them. Device identity and production authorization are issued later by
# Cloud Ops after the edge first joins the bootstrap tailnet.
#
set -euo pipefail

EDGE_ROOT="/opt/Drycontroller-edge"
INSTALL_PACKAGES=true
INSTALL_DOCKER=true
INSTALL_TAILSCALE=true
ENABLE_LEGACY_BOOTSTRAP=false
START_LEGACY_BOOTSTRAP=false
REPO_SOURCE=""

usage() {
  cat <<'EOF'
Usage:
  sudo bash infra/bootstrap/install-edge-host.sh [options]

Options:
  --edge-root PATH       Install edge project here. Default: /opt/Drycontroller-edge
  --source PATH          Source repo/package directory. Default: repository root containing this script
  --skip-packages        Do not run apt-get; only verify prerequisites
  --skip-docker          Do not install Docker; only verify it exists
  --skip-tailscale       Do not install Tailscale; only verify it exists
  --enable-legacy-bootstrap
                         Enable drycontroller-bootstrap.service for the old Provisioning USB path
  --start-legacy-bootstrap
                         Start drycontroller-bootstrap.service after installing it
  -h, --help             Show this help

Expected follow-up:
  Install factory license + /etc/drycontroller/tailnet.env in the OS image,
  then boot with network access to Headscale/Cloud Ops tailnet activation.
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
    --edge-root)
      EDGE_ROOT="${2:-}"
      [ -n "$EDGE_ROOT" ] || die "--edge-root requires a value"
      shift 2
      ;;
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
    --no-enable-service)
      ENABLE_LEGACY_BOOTSTRAP=false
      shift
      ;;
    --enable-legacy-bootstrap)
      ENABLE_LEGACY_BOOTSTRAP=true
      shift
      ;;
    --start-bootstrap|--start-legacy-bootstrap)
      ENABLE_LEGACY_BOOTSTRAP=true
      START_LEGACY_BOOTSTRAP=true
      shift
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
command -v systemctl >/dev/null 2>&1 || die "systemd is required"

if [ -z "$REPO_SOURCE" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  REPO_SOURCE="$(cd "${SCRIPT_DIR}/../.." && pwd)"
else
  REPO_SOURCE="$(cd "$REPO_SOURCE" && pwd)"
fi

[ -f "${REPO_SOURCE}/docker-compose.yml" ] || die "docker-compose.yml not found in source: ${REPO_SOURCE}"
[ -f "${REPO_SOURCE}/env.example" ] || die "env.example not found in source: ${REPO_SOURCE}"
[ -f "${REPO_SOURCE}/infra/bootstrap/drycontroller-bootstrap.sh" ] || die "bootstrap script missing in source"
[ -f "${REPO_SOURCE}/infra/bootstrap/drycontroller-bootstrap.service" ] || die "bootstrap service missing in source"
[ -f "${REPO_SOURCE}/infra/bootstrap/drycontroller-host-tailnet.sh" ] || die "host tailnet script missing in source"
[ -f "${REPO_SOURCE}/infra/bootstrap/drycontroller-host-enroll.sh" ] || die "host enroll script missing in source"
[ -f "${REPO_SOURCE}/infra/bootstrap/drycontroller-host-activation.sh" ] || die "host activation script missing in source"

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

  # Mainland China: pkgs.tailscale.com is unreachable from the field and there is
  # no usable public tailscale apt mirror, so we do not attempt a network install.
  # Provision hosts from an OS image with tailscale preinstalled.
  die "tailscale is not installed; provision this host from an OS image with tailscale preinstalled"
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

install_bootstrap_timeout_override() {
  mkdir -p /etc/systemd/system/drycontroller-bootstrap.service.d
  cat > /etc/systemd/system/drycontroller-bootstrap.service.d/override.conf <<'EOF'
[Service]
TimeoutStartSec=1800
EOF
  log "installed bootstrap timeout override: TimeoutStartSec=1800"
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

install_bootstrap_service() {
  install -m 0755 "${EDGE_ROOT}/infra/bootstrap/drycontroller-bootstrap.sh" /usr/local/sbin/drycontroller-bootstrap.sh
  install -m 0755 "${EDGE_ROOT}/infra/bootstrap/drycontroller-host-tailnet.sh" /usr/local/sbin/drycontroller-host-tailnet.sh
  install -m 0755 "${EDGE_ROOT}/infra/bootstrap/drycontroller-host-enroll.sh" /usr/local/sbin/drycontroller-host-enroll.sh
  install -m 0755 "${EDGE_ROOT}/infra/bootstrap/drycontroller-host-activation.sh" /usr/local/sbin/drycontroller-host-activation.sh
  install -m 0644 "${EDGE_ROOT}/infra/bootstrap/drycontroller-bootstrap.service" /etc/systemd/system/drycontroller-bootstrap.service
  install -m 0644 "${EDGE_ROOT}/infra/bootstrap/drycontroller-tailnet.service" /etc/systemd/system/drycontroller-tailnet.service
  install -m 0644 "${EDGE_ROOT}/infra/bootstrap/drycontroller-enroll.service" /etc/systemd/system/drycontroller-enroll.service
  install -m 0644 "${EDGE_ROOT}/infra/bootstrap/drycontroller-activation.service" /etc/systemd/system/drycontroller-activation.service

  mkdir -p /etc/drycontroller/keys /etc/drycontroller/license /var/lib/drycontroller /var/log
  chmod 755 /etc/drycontroller /etc/drycontroller/keys
  chmod 750 /etc/drycontroller/license
  chmod 700 /var/lib/drycontroller

  systemctl daemon-reload
  systemctl enable drycontroller-tailnet.service drycontroller-activation.service
  log "enabled drycontroller-tailnet.service and drycontroller-activation.service"

  if [ "$ENABLE_LEGACY_BOOTSTRAP" = true ]; then
    systemctl enable drycontroller-bootstrap.service
    log "enabled legacy drycontroller-bootstrap.service"
  else
    log "legacy bootstrap service installed but not enabled"
  fi

  if [ "$START_LEGACY_BOOTSTRAP" = true ]; then
    log "starting legacy drycontroller-bootstrap.service"
    systemctl start drycontroller-bootstrap.service
  fi
}

write_host_manifest() {
  mkdir -p /var/lib/drycontroller
  cat > /var/lib/drycontroller/host-install.json <<EOF
{
  "installed_at": "$(date -Is)",
    "edge_root": "${EDGE_ROOT}",
    "source": "${REPO_SOURCE}",
    "bootstrap_service": "drycontroller-bootstrap.service",
    "tailnet_service": "drycontroller-tailnet.service",
    "activation_service": "drycontroller-activation.service",
    "enroll_service": "drycontroller-enroll.service",
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
install_bootstrap_service
install_bootstrap_timeout_override
write_host_manifest

log "host preparation complete"
log "next: boot with factory license and /etc/drycontroller/tailnet.env installed"
log "legacy Provisioning USB path remains available via: systemctl start drycontroller-bootstrap.service"
