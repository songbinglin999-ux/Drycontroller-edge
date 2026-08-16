#!/usr/bin/env bash
# Derive and package only the public H/S release trust roots from CI secrets.
set -euo pipefail

: "${HOST_COSIGN_PRIVATE_KEY:?missing HOST_COSIGN_PRIVATE_KEY}"
: "${HOST_COSIGN_PASSWORD:?missing HOST_COSIGN_PASSWORD}"
: "${SOFTWARE_COSIGN_PRIVATE_KEY:?missing SOFTWARE_COSIGN_PRIVATE_KEY}"
: "${SOFTWARE_COSIGN_PASSWORD:?missing SOFTWARE_COSIGN_PASSWORD}"
: "${RELEASE_SOURCE_COMMIT:?missing RELEASE_SOURCE_COMMIT}"
: "${RELEASE_HANDOFF_OUTPUT_DIR:?missing RELEASE_HANDOFF_OUTPUT_DIR}"

[[ "${RELEASE_SOURCE_COMMIT}" =~ ^[0-9a-f]{40}$ ]]
test -d "${RELEASE_HANDOFF_OUTPUT_DIR}"
test ! -L "${RELEASE_HANDOFF_OUTPUT_DIR}"
command -v cosign >/dev/null
command -v python3 >/dev/null
bash scripts/ci/verify-cosign-toolchain.sh

work_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/drycontroller-release-public-keys.XXXXXX")"
chmod 700 "${work_dir}"
host_private_key="${work_dir}/host-cosign.key"
software_private_key="${work_dir}/software-cosign.key"
host_public_key="${work_dir}/host-cosign.pub"
software_public_key="${work_dir}/software-cosign.pub"

cleanup() {
  rm -rf -- "${work_dir}"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

printf '%s' "${HOST_COSIGN_PRIVATE_KEY}" > "${host_private_key}"
printf '%s' "${SOFTWARE_COSIGN_PRIVATE_KEY}" > "${software_private_key}"
chmod 600 "${host_private_key}" "${software_private_key}"
COSIGN_PASSWORD="${HOST_COSIGN_PASSWORD}" cosign public-key \
  --key "${host_private_key}" > "${host_public_key}"
COSIGN_PASSWORD="${SOFTWARE_COSIGN_PASSWORD}" cosign public-key \
  --key "${software_private_key}" > "${software_public_key}"
chmod 600 "${host_public_key}" "${software_public_key}"
rm -f -- "${host_private_key}" "${software_private_key}"
unset HOST_COSIGN_PRIVATE_KEY HOST_COSIGN_PASSWORD
unset SOFTWARE_COSIGN_PRIVATE_KEY SOFTWARE_COSIGN_PASSWORD

python3 scripts/ci/prepare-release-handoff.py keys \
  --host-public-key "${host_public_key}" \
  --software-public-key "${software_public_key}" \
  --source-commit "${RELEASE_SOURCE_COMMIT}" \
  --output-dir "${RELEASE_HANDOFF_OUTPUT_DIR}"
