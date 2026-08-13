#!/usr/bin/env bash
# Build one paired H/T/S transaction and complete HostRelease last.
set -euo pipefail

: "${HOST_RELEASE_VERSION:?missing HOST_RELEASE_VERSION}"
: "${SOFTWARE_RELEASE_VERSION:?missing SOFTWARE_RELEASE_VERSION}"
: "${RELEASE_SOURCE_COMMIT:?missing RELEASE_SOURCE_COMMIT}"
: "${HARBOR_HOST:?missing HARBOR_HOST}"
: "${HARBOR_PROJECT:?missing HARBOR_PROJECT}"
: "${HARBOR_USER:?missing HARBOR_USER}"
: "${HARBOR_PASSWORD:?missing HARBOR_PASSWORD}"
: "${HOST_COSIGN_PRIVATE_KEY:?missing HOST_COSIGN_PRIVATE_KEY}"
: "${HOST_COSIGN_PASSWORD:?missing HOST_COSIGN_PASSWORD}"
: "${SOFTWARE_COSIGN_PRIVATE_KEY:?missing SOFTWARE_COSIGN_PRIVATE_KEY}"
: "${SOFTWARE_COSIGN_PASSWORD:?missing SOFTWARE_COSIGN_PASSWORD}"
: "${CLOUD_OPS_RELEASE_API_URL:?missing CLOUD_OPS_RELEASE_API_URL}"
: "${CLOUD_OPS_RELEASE_API_KEY:?missing CLOUD_OPS_RELEASE_API_KEY}"
: "${TRIVY_CACHE_DIR:?missing TRIVY_CACHE_DIR}"

[[ "${HOST_RELEASE_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$ ]]
[[ "${SOFTWARE_RELEASE_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$ ]]
[[ "${RELEASE_SOURCE_COMMIT}" =~ ^[0-9a-f]{40}$ ]]
[[ "${HARBOR_HOST}" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?$ ]]
[[ "${HARBOR_PROJECT}" =~ ^[a-z0-9]+([._-][a-z0-9]+)*$ ]]
[[ "${CLOUD_OPS_RELEASE_API_URL}" == https://* ]]
for command in docker trivy cosign python3 tar sha256sum; do
  command -v "${command}" >/dev/null
done
bash scripts/ci/verify-cosign-toolchain.sh
test -f "${TRIVY_CACHE_DIR}/db/trivy.db"
test -f "${TRIVY_CACHE_DIR}/db/metadata.json"

work_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/drycontroller-host-release.XXXXXX")"
chmod 700 "${work_dir}"
host_private_key="${work_dir}/host-cosign.key"
host_public_key="${work_dir}/host-cosign.pub"
software_private_key="${work_dir}/software-cosign.key"
software_public_key="${work_dir}/software-cosign.pub"
signing_config="${work_dir}/cosign-signing-config.json"
host_release_file="${work_dir}/host-release.json"
software_release_file="${work_dir}/software-release.json"
topology_file="${work_dir}/topology.json"
manifest_file="${work_dir}/host-manifest.json"
manifest_signature="${work_dir}/host-manifest.sigstore.json"
bundle_parent="${work_dir}/bundle"
bundle_root="${bundle_parent}/host-release"
bundle_tar="${work_dir}/host-bundle.tar"
bundle_verification_archive="${work_dir}/host-bundle-image.tar"
export DOCKER_CONFIG="${work_dir}/docker"
mkdir -p "${DOCKER_CONFIG}"
chmod 700 "${DOCKER_CONFIG}"

cleanup() {
  if [[ -n "${bundle_target:-}" ]]; then
    docker image rm -- "${bundle_target}" >/dev/null 2>&1 || true
  fi
  rm -rf -- "${work_dir}"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

printf '%s' "${HOST_COSIGN_PRIVATE_KEY}" > "${host_private_key}"
printf '%s' "${SOFTWARE_COSIGN_PRIVATE_KEY}" > "${software_private_key}"
chmod 600 "${host_private_key}" "${software_private_key}"
COSIGN_PASSWORD="${HOST_COSIGN_PASSWORD}" cosign public-key --key "${host_private_key}" > "${host_public_key}"
COSIGN_PASSWORD="${SOFTWARE_COSIGN_PASSWORD}" cosign public-key --key "${software_private_key}" > "${software_public_key}"
chmod 600 "${host_public_key}" "${software_public_key}"
host_key_digest="$(sha256sum "${host_public_key}" | awk '{print $1}')"
software_key_digest="$(sha256sum "${software_public_key}" | awk '{print $1}')"
test "${host_key_digest}" != "${software_key_digest}"
COSIGN_PASSWORD="${HOST_COSIGN_PASSWORD}" cosign signing-config create \
  --no-default-fulcio \
  --no-default-oidc \
  --no-default-rekor \
  --no-default-tsa \
  --out "${signing_config}"
chmod 600 "${signing_config}"

python3 - <<'PY'
import base64
import json
import os
from pathlib import Path

auth = base64.b64encode(f"{os.environ['HARBOR_USER']}:{os.environ['HARBOR_PASSWORD']}".encode()).decode()
path = Path(os.environ["DOCKER_CONFIG"]) / "config.json"
path.write_text(json.dumps({"auths": {os.environ["HARBOR_HOST"]: {"auth": auth}}}, separators=(",", ":")), encoding="utf-8")
path.chmod(0o600)
PY

topology_digest="$(
  python3 scripts/prepare-host-release.py stage \
    --source-root . \
    --output-root "${bundle_root}" \
    --topology-output "${topology_file}"
)"
[[ "${topology_digest}" =~ ^sha256:[0-9a-f]{64}$ ]]

python3 - "${host_release_file}" <<'PY'
import json
import os
import sys
import urllib.request
from pathlib import Path

body = json.dumps(
    {"version": os.environ["HOST_RELEASE_VERSION"], "source_commit": os.environ["RELEASE_SOURCE_COMMIT"]},
    separators=(",", ":"),
).encode()
request = urllib.request.Request(
    os.environ["CLOUD_OPS_RELEASE_API_URL"].rstrip("/") + "/api/v1/host-releases",
    data=body,
    headers={"Content-Type": "application/json", "X-API-Key": os.environ["CLOUD_OPS_RELEASE_API_KEY"]},
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    payload = json.loads(response.read(1024 * 1024))
if payload.get("status") != "building" or not payload.get("id") or int(payload.get("sequence") or 0) < 1:
    raise SystemExit("Cloud did not create a building HostRelease")
Path(sys.argv[1]).write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
Path(sys.argv[1]).chmod(0o600)
PY

host_release_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["id"])' "${host_release_file}")"
export HOST_RELEASE_ID="${host_release_id}"
export TOPOLOGY_DIGEST="${topology_digest}"
export RELEASE_VERSION="${SOFTWARE_RELEASE_VERSION}"
export SOFTWARE_RELEASE_OUTPUT="${software_release_file}"
export COSIGN_PRIVATE_KEY="${SOFTWARE_COSIGN_PRIVATE_KEY}"
export COSIGN_PASSWORD="${SOFTWARE_COSIGN_PASSWORD}"
bash scripts/ci/build-software-release.sh

tar \
  --sort=name \
  --mtime='UTC 1970-01-01' \
  --owner=0 \
  --group=0 \
  --numeric-owner \
  --format=posix \
  -C "${bundle_parent}" \
  -cf "${bundle_tar}" \
  host-release
bundle_target="${HARBOR_HOST}/${HARBOR_PROJECT}/host-bundle:${HOST_RELEASE_VERSION}"
docker import --platform linux/amd64 "${bundle_tar}" "${bundle_target}" >/dev/null
trivy image \
  --cache-dir "${TRIVY_CACHE_DIR}" \
  --skip-db-update \
  --ignore-unfixed \
  --severity CRITICAL,HIGH \
  --exit-code 1 \
  --no-progress \
  --timeout 15m \
  "${bundle_target}"
docker push "${bundle_target}"
bundle_repository="${bundle_target%:*}"
bundle_image="$(
  docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${bundle_target}" \
    | awk -v prefix="${bundle_repository}@" 'index($0, prefix) == 1 { print; exit }'
)"
[[ "${bundle_image}" == "${bundle_repository}@sha256:"* ]]
docker image save -o "${bundle_verification_archive}" "${bundle_target}"
bundle_image_id="$(
  PYTHONPATH="infra/ota-helper" python3 - "${bundle_verification_archive}" <<'PY'
import sys
from pathlib import Path

from release_engine import docker_archive_config_digest

print(docker_archive_config_digest(Path(sys.argv[1])))
PY
)"
rm -f -- "${bundle_verification_archive}"
[[ "${bundle_image_id}" =~ ^sha256:[0-9a-f]{64}$ ]]
COSIGN_PASSWORD="${HOST_COSIGN_PASSWORD}" cosign sign \
  --yes \
  --signing-config "${signing_config}" \
  --key "${host_private_key}" \
  "${bundle_image}"
cosign verify --insecure-ignore-tlog=true --key "${host_public_key}" "${bundle_image}" >/dev/null

python3 scripts/prepare-host-release.py manifest \
  --root "${bundle_root}" \
  --topology "${topology_file}" \
  --host-release "${host_release_file}" \
  --software-release "${software_release_file}" \
  --host-public-key "${host_public_key}" \
  --bundle-image "${bundle_image}" \
  --bundle-image-id "${bundle_image_id}" \
  --output "${manifest_file}"
COSIGN_PASSWORD="${HOST_COSIGN_PASSWORD}" cosign sign-blob \
  --yes \
  --signing-config "${signing_config}" \
  --key "${host_private_key}" \
  --bundle "${manifest_signature}" \
  "${manifest_file}" >/dev/null
cosign verify-blob \
  --key "${host_public_key}" \
  --insecure-ignore-tlog=true \
  --bundle "${manifest_signature}" \
  "${manifest_file}" >/dev/null

python3 - "${host_release_file}" "${manifest_file}" "${manifest_signature}" <<'PY'
import base64
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

release = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
manifest_bytes = Path(sys.argv[2]).read_bytes()
body = json.dumps(
    {
        "manifest": json.loads(manifest_bytes),
        "manifest_signature": base64.b64encode(Path(sys.argv[3]).read_bytes()).decode(),
    },
    separators=(",", ":"),
).encode()
request = urllib.request.Request(
    os.environ["CLOUD_OPS_RELEASE_API_URL"].rstrip("/")
    + f"/api/v1/host-releases/{release['id']}/complete",
    data=body,
    headers={"Content-Type": "application/json", "X-API-Key": os.environ["CLOUD_OPS_RELEASE_API_KEY"]},
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    payload = json.loads(response.read(4 * 1024 * 1024))
expected = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
if payload.get("status") != "completed" or payload.get("manifest_digest") != expected:
    raise SystemExit("Cloud did not complete the exact HostRelease manifest")
print(f"HostRelease completed: id={release['id']} digest={expected}")
PY
