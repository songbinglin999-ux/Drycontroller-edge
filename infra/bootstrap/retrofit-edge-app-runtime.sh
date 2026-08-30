#!/usr/bin/env bash
# Install only the active edge-app-v1 host helpers without changing containers or application data.
set -euo pipefail

readonly COSIGN_APPROVED_VERSION="v3.1.3"
readonly COSIGN_APPROVED_LINUX_AMD64_SHA256="4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71"
readonly LEGACY_UNIT="drycontroller-host-release-runner.service"
readonly LEGACY_UNIT_PATH="/etc/systemd/system/${LEGACY_UNIT}"
readonly LEGACY_ARCHIVE="/var/lib/drycontroller/legacy-host-release/${LEGACY_UNIT}"
readonly LEGACY_SOCKET="/run/drycontroller-host-release/runner.sock"

REPO_SOURCE=""
SOFTWARE_COSIGN_PUBLIC_KEY=""
SOFTWARE_COSIGN_PUBLIC_KEY_SHA256=""
COSIGN_BIN=""

fail() {
  printf '[edge-app-runtime-retrofit] %s\n' "$1" >&2
  exit "${2:-1}"
}

usage() {
  cat <<'EOF'
Usage:
  sudo infra/bootstrap/retrofit-edge-app-runtime.sh \
    --source PATH \
    --software-cosign-public-key FILE \
    --software-cosign-public-key-sha256 SHA256 \
    --cosign-bin FILE

The retrofit reuses the installed device-identity runtime and installs the TPM
observer, License transaction recovery, registry credential writer, edge-app-v1
recovery, and OTA helper. It does not pull or replace images, create an active
baseline, issue a License, or alter Docker data. The legacy HostRelease runner
is stopped, archived if present, and masked.
EOF
}

required_value() {
  [ -n "${2:-}" ] || fail "$1 requires a value" 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source) required_value "$1" "${2:-}"; REPO_SOURCE="$2"; shift 2 ;;
    --software-cosign-public-key) required_value "$1" "${2:-}"; SOFTWARE_COSIGN_PUBLIC_KEY="$2"; shift 2 ;;
    --software-cosign-public-key-sha256) required_value "$1" "${2:-}"; SOFTWARE_COSIGN_PUBLIC_KEY_SHA256="$2"; shift 2 ;;
    --cosign-bin) required_value "$1" "${2:-}"; COSIGN_BIN="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; fail "unknown argument: $1" 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || fail "retrofit must run as root" 2
[ "$(uname -s)" = "Linux" ] && [ "$(uname -m)" = "x86_64" ] \
  || fail "Linux amd64 is required" 2
[ -c /dev/tpmrm0 ] || fail "TPM 2.0 resource manager /dev/tpmrm0 is required" 3
command -v systemctl >/dev/null 2>&1 || fail "systemd is required" 2
command -v python3 >/dev/null 2>&1 || fail "python3 is required" 2
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required" 2

REPO_SOURCE="$(cd "${REPO_SOURCE}" && pwd -P)"
for path in \
  "${SOFTWARE_COSIGN_PUBLIC_KEY}" \
  "${COSIGN_BIN}" \
  "${REPO_SOURCE}/infra/bootstrap/install-edge-app-runtime.py"; do
  [ -f "${path}" ] && [ ! -L "${path}" ] || fail "required regular input missing: ${path}" 2
done
[ -x "${COSIGN_BIN}" ] || fail "cosign input must be executable" 2
[ -d /opt/drycontroller-identity ] && [ ! -L /opt/drycontroller-identity ] \
  || fail "installed device identity runtime is unavailable" 3
[ -d /opt/drycontroller-identity/identity_service ] \
  && [ ! -L /opt/drycontroller-identity/identity_service ] \
  && [ -x /opt/drycontroller-identity/venv/bin/python ] \
  || fail "installed device identity runtime is incomplete" 3
PYTHONPATH=/opt/drycontroller-identity \
  /opt/drycontroller-identity/venv/bin/python -c \
  'from identity_service.backend import TpmIdentityBackend' \
  || fail "installed TPM identity backend is unavailable" 3

[[ "${SOFTWARE_COSIGN_PUBLIC_KEY_SHA256}" =~ ^[a-f0-9]{64}$ ]] \
  || fail "software cosign public key SHA-256 is invalid" 2
[ "$(sha256sum "${SOFTWARE_COSIGN_PUBLIC_KEY}" | awk '{print $1}')" = "${SOFTWARE_COSIGN_PUBLIC_KEY_SHA256}" ] \
  || fail "software cosign public key checksum is not approved" 2

actual_cosign_sha256="$(sha256sum "${COSIGN_BIN}" | awk '{print $1}')"
[ "${actual_cosign_sha256}" = "${COSIGN_APPROVED_LINUX_AMD64_SHA256}" ] \
  || fail "cosign checksum is not approved" 2
cosign_version="$(${COSIGN_BIN} version 2>/dev/null)" || fail "cosign version is unavailable" 2
grep -Eq "GitVersion:[[:space:]]+${COSIGN_APPROVED_VERSION//./\\.}([[:space:]]|$)" <<<"${cosign_version}" \
  || fail "cosign version is not approved" 2
grep -Eq 'Platform:[[:space:]]+linux/amd64([[:space:]]|$)' <<<"${cosign_version}" \
  || fail "cosign platform is not approved" 2

python3 "${REPO_SOURCE}/infra/bootstrap/install-edge-app-runtime.py" \
  --repo-source "${REPO_SOURCE}" \
  --software-cosign-public-key "${SOFTWARE_COSIGN_PUBLIC_KEY}" \
  --approved-software-public-key-sha256 "${SOFTWARE_COSIGN_PUBLIC_KEY_SHA256}" \
  --cosign-binary "${COSIGN_BIN}" \
  --approved-cosign-sha256 "${COSIGN_APPROVED_LINUX_AMD64_SHA256}"

systemctl disable --now drycontroller-ota-helper.service >/dev/null 2>&1 || true
systemctl disable --now "${LEGACY_UNIT}" >/dev/null 2>&1 || true
systemctl is-active --quiet "${LEGACY_UNIT}" && fail "legacy HostRelease runner is still active" 5
if [ -e "${LEGACY_SOCKET}" ] || [ -L "${LEGACY_SOCKET}" ]; then
  [ -S "${LEGACY_SOCKET}" ] || fail "legacy HostRelease socket path is unsafe" 5
  rm -f -- "${LEGACY_SOCKET}"
fi
install -d -o root -g root -m 0700 "$(dirname "${LEGACY_ARCHIVE}")"
if [ -e "${LEGACY_UNIT_PATH}" ] && [ ! -L "${LEGACY_UNIT_PATH}" ]; then
  [ -f "${LEGACY_UNIT_PATH}" ] || fail "legacy HostRelease unit path is unsafe" 5
  [ ! -e "${LEGACY_ARCHIVE}" ] || cmp -s "${LEGACY_UNIT_PATH}" "${LEGACY_ARCHIVE}" \
    || fail "legacy HostRelease unit archive already differs" 5
  [ -e "${LEGACY_ARCHIVE}" ] || install -o root -g root -m 0600 "${LEGACY_UNIT_PATH}" "${LEGACY_ARCHIVE}"
  [ ! -e "${LEGACY_UNIT_PATH}.retired" ] \
    || fail "legacy HostRelease retired unit path already exists" 5
  mv "${LEGACY_UNIT_PATH}" "${LEGACY_UNIT_PATH}.retired"
fi
if [ -L "${LEGACY_UNIT_PATH}" ]; then
  [ "$(readlink "${LEGACY_UNIT_PATH}")" = /dev/null ] || fail "legacy HostRelease unit mask is unsafe" 5
else
  ln -s /dev/null "${LEGACY_UNIT_PATH}"
fi

systemctl daemon-reload
systemctl enable --now drycontroller-tpm-identity-observer.service
systemctl enable --now drycontroller-tailnet-observer.timer
systemctl enable drycontroller-license-transaction-recovery.service
systemctl enable drycontroller-registry-auth-writer.service
systemctl is-enabled --quiet "${LEGACY_UNIT}" && fail "legacy HostRelease unit was re-enabled" 5
[ ! -S "${LEGACY_SOCKET}" ] || fail "legacy HostRelease socket remained after retirement" 5
[ -s /run/drycontroller-identity/tpm-observation.json ] \
  || fail "live TPM observation was not produced" 5

printf '%s\n' \
  'edge-app-v1 runtime installed but disabled; initialize a signed baseline before enabling recovery and OTA helper'
