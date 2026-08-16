#!/usr/bin/env bash
# Rebuild a public pair handoff from two immutable completed Cloud release rows.
set -euo pipefail

: "${HOST_RELEASE_ID:?missing HOST_RELEASE_ID}"
: "${SOFTWARE_RELEASE_ID:?missing SOFTWARE_RELEASE_ID}"
: "${RELEASE_SOURCE_COMMIT:?missing RELEASE_SOURCE_COMMIT}"
: "${HOST_COSIGN_PRIVATE_KEY:?missing HOST_COSIGN_PRIVATE_KEY}"
: "${HOST_COSIGN_PASSWORD:?missing HOST_COSIGN_PASSWORD}"
: "${SOFTWARE_COSIGN_PRIVATE_KEY:?missing SOFTWARE_COSIGN_PRIVATE_KEY}"
: "${SOFTWARE_COSIGN_PASSWORD:?missing SOFTWARE_COSIGN_PASSWORD}"
: "${CLOUD_OPS_RELEASE_API_URL:?missing CLOUD_OPS_RELEASE_API_URL}"
: "${CLOUD_OPS_RELEASE_API_KEY:?missing CLOUD_OPS_RELEASE_API_KEY}"
: "${RELEASE_HANDOFF_OUTPUT_DIR:?missing RELEASE_HANDOFF_OUTPUT_DIR}"

[[ "${HOST_RELEASE_ID}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]
[[ "${SOFTWARE_RELEASE_ID}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]
[[ "${RELEASE_SOURCE_COMMIT}" =~ ^[0-9a-f]{40}$ ]]
[[ "${CLOUD_OPS_RELEASE_API_URL}" == https://* ]]
test -d "${RELEASE_HANDOFF_OUTPUT_DIR}"
test ! -L "${RELEASE_HANDOFF_OUTPUT_DIR}"
test -z "$(find "${RELEASE_HANDOFF_OUTPUT_DIR}" -mindepth 1 -maxdepth 1 -print -quit)"
for command in cosign python3; do
  command -v "${command}" >/dev/null
done
bash scripts/ci/verify-cosign-toolchain.sh

work_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/drycontroller-release-recovery.XXXXXX")"
chmod 700 "${work_dir}"
host_private_key="${work_dir}/host-cosign.key"
host_public_key="${work_dir}/host-cosign.pub"
software_private_key="${work_dir}/software-cosign.key"
software_public_key="${work_dir}/software-cosign.pub"
host_release_file="${work_dir}/host-release.json"
software_release_file="${work_dir}/software-release.json"

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

# This authenticated observation must pass before the Release publisher can mutate Gitea.
python3 scripts/ci/prepare-release-handoff.py trust-preflight \
  --host-public-key "${host_public_key}" \
  --software-public-key "${software_public_key}" \
  --cloud-url "${CLOUD_OPS_RELEASE_API_URL}" \
  --cloud-api-key "${CLOUD_OPS_RELEASE_API_KEY}"

python3 - "${host_release_file}" "${software_release_file}" <<'PY'
import json
import os
from pathlib import Path
import sys
import urllib.parse
import urllib.request


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


origin = os.environ["CLOUD_OPS_RELEASE_API_URL"].rstrip("/")
parsed = urllib.parse.urlsplit(origin)
if parsed.scheme != "https" or not parsed.netloc or parsed.path:
    raise SystemExit("Cloud Ops release URL must be one HTTPS origin")
headers = {"X-API-Key": os.environ["CLOUD_OPS_RELEASE_API_KEY"]}
opener = urllib.request.build_opener(RejectRedirects())
for kind, release_id, destination in (
    ("host-releases", os.environ["HOST_RELEASE_ID"], Path(sys.argv[1])),
    ("software-releases", os.environ["SOFTWARE_RELEASE_ID"], Path(sys.argv[2])),
):
    request = urllib.request.Request(
        f"{origin}/api/v1/{kind}/{release_id}", headers=headers, method="GET"
    )
    with opener.open(request, timeout=30) as response:
        raw = response.read(4 * 1024 * 1024)
    payload = json.loads(raw)
    if (
        payload.get("id") != release_id
        or payload.get("status") != "completed"
        or payload.get("source_commit") != os.environ["RELEASE_SOURCE_COMMIT"]
        or not isinstance(payload.get("manifest"), dict)
        or not payload.get("manifest_digest")
        or not payload.get("manifest_signature")
    ):
        raise SystemExit(f"Cloud {kind} row is not the exact completed recovery source")
    destination.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    destination.chmod(0o600)
PY

rm -f -- "${host_private_key}" "${software_private_key}"
unset HOST_COSIGN_PRIVATE_KEY HOST_COSIGN_PASSWORD
unset SOFTWARE_COSIGN_PRIVATE_KEY SOFTWARE_COSIGN_PASSWORD
unset CLOUD_OPS_RELEASE_API_KEY

python3 scripts/ci/prepare-release-handoff.py pair \
  --host-release "${host_release_file}" \
  --software-release "${software_release_file}" \
  --host-public-key "${host_public_key}" \
  --software-public-key "${software_public_key}" \
  --cosign "$(command -v cosign)" \
  --output-dir "${RELEASE_HANDOFF_OUTPUT_DIR}"
