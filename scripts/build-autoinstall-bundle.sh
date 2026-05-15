#!/usr/bin/env bash
#
# Build files that must be copied to the root of an Ubuntu Server autoinstall
# USB/ISO. This does not write to block devices.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUT_DIR="${REPO_ROOT}/dist/autoinstall"

usage() {
  cat <<'EOF'
Usage:
  bash scripts/build-autoinstall-bundle.sh [--out DIR]

Output:
  DIR/user-data
  DIR/meta-data
  DIR/post-install.sh
  DIR/drycontroller-edge-post-install.service
  DIR/wifi.env.example
  DIR/drycontroller-edge.tar.gz

Copy those four files to the root of Ubuntu Server installer media and boot with:
  autoinstall ds=nocloud;s=/cdrom/
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --out)
      OUT_DIR="${2:-}"
      [ -n "$OUT_DIR" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

mkdir -p "$OUT_DIR"

install -m 0644 "${REPO_ROOT}/infra/autoinstall/user-data" "${OUT_DIR}/user-data"
install -m 0644 "${REPO_ROOT}/infra/autoinstall/meta-data" "${OUT_DIR}/meta-data"
install -m 0755 "${REPO_ROOT}/infra/autoinstall/post-install.sh" "${OUT_DIR}/post-install.sh"
install -m 0644 "${REPO_ROOT}/infra/autoinstall/drycontroller-edge-post-install.service" "${OUT_DIR}/drycontroller-edge-post-install.service"
install -m 0600 "${REPO_ROOT}/infra/autoinstall/wifi.env.example" "${OUT_DIR}/wifi.env.example"

(
  cd "$REPO_ROOT"
  tar \
    --exclude='.git' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='edge-local.env' \
    --exclude='.venv' \
    --exclude='.venv*' \
    --exclude='.venv-*' \
    --exclude='node_modules' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='dist' \
    --exclude='logs' \
    --exclude='data' \
    --exclude='tmp' \
    --exclude='.DS_Store' \
    --exclude='*.pyc' \
    -czf "${OUT_DIR}/drycontroller-edge.tar.gz" .
)

cat <<EOF
Autoinstall bundle written to:
  ${OUT_DIR}

Required files:
  ${OUT_DIR}/user-data
  ${OUT_DIR}/meta-data
  ${OUT_DIR}/post-install.sh
  ${OUT_DIR}/drycontroller-edge-post-install.service
  ${OUT_DIR}/wifi.env.example
  ${OUT_DIR}/drycontroller-edge.tar.gz

Before production use:
  1. Replace the placeholder password hash in user-data.
  2. If the install area uses WiFi, copy wifi.env.example to wifi.env and fill it.
EOF
