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
DEVICE_IDENTITY_CLOUD_PUBLIC_KEY=""
DEVICE_IDENTITY_CLOUD_PUBLIC_KEY_SHA256=""
COSIGN_BIN=""
IDENTITY_WHEELHOUSE=""

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
    --device-identity-cloud-key FILE \
    --device-identity-cloud-key-sha256 SHA256 \
    --cosign-bin FILE \
    --identity-wheelhouse DIR

The retrofit installs the TPM observer, License transaction recovery, registry
credential writer, edge-app-v1 recovery, and OTA helper. It does not pull or
replace images, create an active baseline, issue a License, or alter Docker data.
The legacy HostRelease runner is stopped, archived if present, and masked.
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
    --device-identity-cloud-key) required_value "$1" "${2:-}"; DEVICE_IDENTITY_CLOUD_PUBLIC_KEY="$2"; shift 2 ;;
    --device-identity-cloud-key-sha256) required_value "$1" "${2:-}"; DEVICE_IDENTITY_CLOUD_PUBLIC_KEY_SHA256="$2"; shift 2 ;;
    --cosign-bin) required_value "$1" "${2:-}"; COSIGN_BIN="$2"; shift 2 ;;
    --identity-wheelhouse) required_value "$1" "${2:-}"; IDENTITY_WHEELHOUSE="$2"; shift 2 ;;
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
  "${DEVICE_IDENTITY_CLOUD_PUBLIC_KEY}" \
  "${COSIGN_BIN}" \
  "${REPO_SOURCE}/infra/bootstrap/install-static-runtime.py" \
  "${REPO_SOURCE}/services/device-identity/requirements.txt"; do
  [ -f "${path}" ] && [ ! -L "${path}" ] || fail "required regular input missing: ${path}" 2
done
[ -x "${COSIGN_BIN}" ] || fail "cosign input must be executable" 2
[ -d "${IDENTITY_WHEELHOUSE}" ] && [ ! -L "${IDENTITY_WHEELHOUSE}" ] \
  || fail "identity wheelhouse is unavailable" 2
[ -d "${REPO_SOURCE}/services/device-identity/identity_service" ] \
  || fail "device identity source is unavailable" 2
[ ! -e /opt/drycontroller-identity ] \
  || fail "existing identity runtime requires separate review; refusing to overwrite" 3

[[ "${SOFTWARE_COSIGN_PUBLIC_KEY_SHA256}" =~ ^[a-f0-9]{64}$ ]] \
  || fail "software cosign public key SHA-256 is invalid" 2
[[ "${DEVICE_IDENTITY_CLOUD_PUBLIC_KEY_SHA256}" =~ ^[a-f0-9]{64}$ ]] \
  || fail "device identity Cloud public key SHA-256 is invalid" 2
[ "$(sha256sum "${SOFTWARE_COSIGN_PUBLIC_KEY}" | awk '{print $1}')" = "${SOFTWARE_COSIGN_PUBLIC_KEY_SHA256}" ] \
  || fail "software cosign public key checksum is not approved" 2
[ "$(sha256sum "${DEVICE_IDENTITY_CLOUD_PUBLIC_KEY}" | awk '{print $1}')" = "${DEVICE_IDENTITY_CLOUD_PUBLIC_KEY_SHA256}" ] \
  || fail "device identity Cloud public key checksum is not approved" 2

actual_cosign_sha256="$(sha256sum "${COSIGN_BIN}" | awk '{print $1}')"
[ "${actual_cosign_sha256}" = "${COSIGN_APPROVED_LINUX_AMD64_SHA256}" ] \
  || fail "cosign checksum is not approved" 2
cosign_version="$(${COSIGN_BIN} version 2>/dev/null)" || fail "cosign version is unavailable" 2
grep -Eq "GitVersion:[[:space:]]+${COSIGN_APPROVED_VERSION//./\\.}([[:space:]]|$)" <<<"${cosign_version}" \
  || fail "cosign version is not approved" 2
grep -Eq 'Platform:[[:space:]]+linux/amd64([[:space:]]|$)' <<<"${cosign_version}" \
  || fail "cosign platform is not approved" 2

identity_stage="$(mktemp -d /opt/.drycontroller-identity.retrofit.XXXXXX)"
cleanup() {
  rm -rf -- "${identity_stage}" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM
install -d -m 0755 "${identity_stage}/common"
install -m 0644 "${REPO_SOURCE}/common/tailscale_status.py" "${identity_stage}/common/tailscale_status.py"
cp -a "${REPO_SOURCE}/services/device-identity/identity_service" "${identity_stage}/identity_service"
find "${identity_stage}/identity_service" -type d -exec chmod 0755 {} +
find "${identity_stage}/identity_service" -type f -exec chmod 0644 {} +
install -m 0644 "${REPO_SOURCE}/services/device-identity/requirements.txt" "${identity_stage}/requirements.txt"
python3 -m venv "${identity_stage}/venv"
"${identity_stage}/venv/bin/pip" install --disable-pip-version-check --no-index \
  --find-links "${IDENTITY_WHEELHOUSE}" -r "${identity_stage}/requirements.txt"
"${identity_stage}/venv/bin/python" -m pip check

python3 "${REPO_SOURCE}/infra/bootstrap/install-static-runtime.py" \
  --repo-source "${REPO_SOURCE}" \
  --software-cosign-public-key "${SOFTWARE_COSIGN_PUBLIC_KEY}" \
  --approved-software-public-key-sha256 "${SOFTWARE_COSIGN_PUBLIC_KEY_SHA256}" \
  --device-identity-cloud-public-key "${DEVICE_IDENTITY_CLOUD_PUBLIC_KEY}" \
  --approved-device-identity-cloud-public-key-sha256 "${DEVICE_IDENTITY_CLOUD_PUBLIC_KEY_SHA256}" \
  --cosign-binary "${COSIGN_BIN}" \
  --approved-cosign-sha256 "${COSIGN_APPROVED_LINUX_AMD64_SHA256}"
mv "${identity_stage}" /opt/drycontroller-identity
identity_stage=""

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
systemctl enable drycontroller-edge-app-recovery.service
systemctl enable drycontroller-ota-helper.service
systemctl is-enabled --quiet "${LEGACY_UNIT}" && fail "legacy HostRelease unit was re-enabled" 5
[ ! -S "${LEGACY_SOCKET}" ] || fail "legacy HostRelease socket remained after retirement" 5
[ -s /run/drycontroller-identity/tpm-observation.json ] \
  || fail "live TPM observation was not produced" 5

printf '%s\n' 'edge-app-v1 runtime retrofit complete; application containers and release state unchanged'
