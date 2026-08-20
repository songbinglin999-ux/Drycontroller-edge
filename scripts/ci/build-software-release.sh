#!/usr/bin/env bash
# Build one complete, signed Edge SoftwareRelease and complete it in Cloud last.
set -euo pipefail

SERVICES=(
  "ai-ctrl-optimizer"
  "ai-softsensor"
  "alertmanager"
  "edge-api"
  "edge-daq"
  "edge-monitor"
  "influxdb"
  "license-service"
  "mosquitto"
  "mosquitto-init"
  "postgres"
  "prometheus"
)
RELEASE_SERVICE_INVENTORY="$(printf '%s\n' "${SERVICES[@]}")"
export RELEASE_SERVICE_INVENTORY

: "${RELEASE_VERSION:?missing RELEASE_VERSION}"
: "${RELEASE_SOURCE_COMMIT:?missing RELEASE_SOURCE_COMMIT}"
: "${HOST_RELEASE_ID:?missing HOST_RELEASE_ID}"
: "${TOPOLOGY_DIGEST:?missing TOPOLOGY_DIGEST}"
: "${HARBOR_HOST:?missing HARBOR_HOST}"
: "${HARBOR_PROJECT:?missing HARBOR_PROJECT}"
: "${HARBOR_USER:?missing HARBOR_USER}"
: "${HARBOR_PASSWORD:?missing HARBOR_PASSWORD}"
: "${COSIGN_PRIVATE_KEY:?missing COSIGN_PRIVATE_KEY}"
: "${COSIGN_PASSWORD:?missing COSIGN_PASSWORD}"
: "${CLOUD_OPS_RELEASE_API_URL:?missing CLOUD_OPS_RELEASE_API_URL}"
: "${CLOUD_OPS_RELEASE_API_KEY:?missing CLOUD_OPS_RELEASE_API_KEY}"
: "${TRIVY_CACHE_DIR:?missing TRIVY_CACHE_DIR}"

[[ "${RELEASE_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$ ]]
[[ "${RELEASE_SOURCE_COMMIT}" =~ ^[0-9a-f]{40}$ ]]
[[ "${HOST_RELEASE_ID}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]
[[ "${TOPOLOGY_DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "${HARBOR_HOST}" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?$ ]]
[[ "${HARBOR_PROJECT}" =~ ^[a-z0-9]+([._-][a-z0-9]+)*$ ]]
[[ "${CLOUD_OPS_RELEASE_API_URL}" == https://* ]]
command -v docker >/dev/null
command -v trivy >/dev/null
command -v cosign >/dev/null
command -v python3 >/dev/null
bash scripts/ci/verify-cosign-toolchain.sh
test -f "${TRIVY_CACHE_DIR}/db/trivy.db"
test -f "${TRIVY_CACHE_DIR}/db/metadata.json"
python3 scripts/ci/python_base_image_contract.py verify-dockerfiles >/dev/null

release_work_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/drycontroller-software-release.XXXXXX")"
chmod 700 "${release_work_dir}"
export DOCKER_CONFIG="${release_work_dir}/docker"
mkdir -p "${DOCKER_CONFIG}"
chmod 700 "${DOCKER_CONFIG}"
signing_key="${release_work_dir}/cosign.key"
signing_pub="${release_work_dir}/cosign.pub"
signing_config="${release_work_dir}/cosign-signing-config.json"
manifest_path="${release_work_dir}/manifest.json"
manifest_signature="${release_work_dir}/manifest.sigstore.json"
image_map_path="${release_work_dir}/images.tsv"
release_metadata_path="${release_work_dir}/cloud-release.json"
predecessor_path="${release_work_dir}/predecessor.json"
predecessor_manifest_path="${release_work_dir}/predecessor-manifest.json"
predecessor_signature_path="${release_work_dir}/predecessor-manifest.sigstore.json"
changed_services_path="${release_work_dir}/changed-services.txt"
inherited_images_path="${release_work_dir}/inherited-images.tsv"
python_base_receipt_dir="${release_work_dir}/python-base-images"
python_base_receipt_path="${release_work_dir}/python-base-image-receipt.json"
mkdir -p "${python_base_receipt_dir}"
chmod 700 "${python_base_receipt_dir}"
declare -A IMAGE_REFS=()
declare -A IMAGE_IDS=()
declare -A CHANGED_SERVICES=()
declare -a LOCAL_TARGETS=()

cleanup() {
  for image in "${LOCAL_TARGETS[@]:-}"; do
    docker image rm -- "${image}" >/dev/null 2>&1 || true
  done
  rm -rf -- "${release_work_dir}"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

python3 - <<'PY'
import base64
import json
import os
from pathlib import Path

auth = base64.b64encode(
    f"{os.environ['HARBOR_USER']}:{os.environ['HARBOR_PASSWORD']}".encode()
).decode()
path = Path(os.environ["DOCKER_CONFIG"]) / "config.json"
path.write_text(
    json.dumps({"auths": {os.environ["HARBOR_HOST"]: {"auth": auth}}}, separators=(",", ":")),
    encoding="utf-8",
)
path.chmod(0o600)
PY
printf '%s' "${COSIGN_PRIVATE_KEY}" > "${signing_key}"
chmod 600 "${signing_key}"
cosign public-key --key "${signing_key}" > "${signing_pub}"
chmod 600 "${signing_pub}"
cosign signing-config create \
  --no-default-fulcio \
  --no-default-oidc \
  --no-default-rekor \
  --no-default-tsa \
  --out "${signing_config}"
chmod 600 "${signing_config}"
export SIGNING_KEY_SHA256="sha256:$(sha256sum "${signing_pub}" | awk '{print $1}')"

release_step_resolve_predecessor() {
  python3 - "${predecessor_path}" <<'PY'
import json
import os
import sys
import urllib.request
from pathlib import Path

request = urllib.request.Request(
    os.environ["CLOUD_OPS_RELEASE_API_URL"].rstrip("/") + "/api/v1/software-releases",
    headers={"X-API-Key": os.environ["CLOUD_OPS_RELEASE_API_KEY"]},
    method="GET",
)
with urllib.request.urlopen(request, timeout=30) as response:
    payload = json.loads(response.read(4 * 1024 * 1024))
if not isinstance(payload, list):
    raise SystemExit("Cloud returned an invalid SoftwareRelease list")
predecessor = next((row for row in payload if row.get("status") == "completed"), None)
Path(sys.argv[1]).write_text(
    json.dumps(predecessor, separators=(",", ":")),
    encoding="utf-8",
)
PY
}

release_step_resolve_changed_services() {
  PYTHONPATH="infra/ota-helper" python3 - "${predecessor_path}" "${changed_services_path}" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

from release_engine import validate_manifest

service_inventory = tuple(os.environ["RELEASE_SERVICE_INVENTORY"].splitlines())
all_services = set(service_inventory)
predecessor = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
changed = set(all_services)

if predecessor is not None:
    manifest = validate_manifest(predecessor.get("manifest"))
    source_commit = predecessor.get("source_commit")
    identity_matches = (
        predecessor.get("id") == manifest["release_id"]
        and predecessor.get("sequence") == manifest["sequence"]
        and predecessor.get("version") == manifest["version"]
        and source_commit == manifest["source_commit"]
        and predecessor.get("manifest_digest")
        and predecessor.get("manifest_signature")
    )
    can_inherit = (
        identity_matches
        and manifest["registry"] == os.environ["HARBOR_HOST"]
        and manifest["signing_key_sha256"] == os.environ["SIGNING_KEY_SHA256"]
        and manifest["host_release_id"] == os.environ["HOST_RELEASE_ID"]
        and manifest["topology_digest"] == os.environ["TOPOLOGY_DIGEST"]
    )
    if can_inherit:
        current_commit = os.environ["RELEASE_SOURCE_COMMIT"]
        try:
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", source_commit, current_commit],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            result = subprocess.run(
                ["git", "diff", "--name-only", "-z", source_commit, current_commit, "--"],
                check=True,
                stdout=subprocess.PIPE,
            )
        except (OSError, subprocess.CalledProcessError):
            pass
        else:
            changed = set()
            for raw_path in result.stdout.split(b"\0"):
                if not raw_path:
                    continue
                path = raw_path.decode("utf-8")
                parts = path.split("/")
                if len(parts) >= 3 and parts[0] == "services" and parts[1] in all_services:
                    changed.add(parts[1])
                elif parts[0] in {"docs", "tests", ".gitea"}:
                    continue
                else:
                    changed = set(all_services)
                    break

Path(sys.argv[2]).write_text(
    "".join(f"{service}\n" for service in service_inventory if service in changed),
    encoding="utf-8",
)
PY
}

release_step_create_cloud() {
  python3 - "${release_metadata_path}" <<'PY'
import json
import os
import sys
import urllib.request
from pathlib import Path

body = json.dumps(
    {
        "version": os.environ["RELEASE_VERSION"],
        "source_commit": os.environ["RELEASE_SOURCE_COMMIT"],
    },
    separators=(",", ":"),
).encode()
request = urllib.request.Request(
    os.environ["CLOUD_OPS_RELEASE_API_URL"].rstrip("/") + "/api/v1/software-releases",
    data=body,
    headers={
        "Content-Type": "application/json",
        "X-API-Key": os.environ["CLOUD_OPS_RELEASE_API_KEY"],
    },
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    payload = json.loads(response.read(1024 * 1024))
if payload.get("status") != "building" or not payload.get("id") or int(payload.get("sequence") or 0) < 1:
    raise SystemExit("Cloud did not create a building SoftwareRelease")
Path(sys.argv[1]).write_text(
    json.dumps({"id": payload["id"], "sequence": int(payload["sequence"])}, separators=(",", ":")),
    encoding="utf-8",
)
PY
}

source_image_for() {
  case "$1" in
    alertmanager) printf '%s\n' "prom/alertmanager:v0.33.1@sha256:a89f8d4520954079275441eecdb71444328bd90633dd4eddfc33b9ed657f349b" ;;
    mosquitto|mosquitto-init) printf '%s\n' "eclipse-mosquitto:2.0.22@sha256:54c90ecc78645241b6aa272b2a5ac8fc20b0eaf02cc4dd431c0cc8d2fd4447dd" ;;
    prometheus) printf '%s\n' "prom/prometheus:v3.13.0@sha256:0e698e35e50d1ddc2d11a4a55b089fe62eb71358a5c204dfafd21bdf8ffe04b8" ;;
    *) return 1 ;;
  esac
}

release_dockerfile_for() {
  case "$1" in
    influxdb) printf '%s\n' "infra/release-images/influxdb/Dockerfile" ;;
    postgres) printf '%s\n' "infra/release-images/postgres/Dockerfile" ;;
    *) return 1 ;;
  esac
}

release_step_build_or_mirror() {
  local service="$1"
  local target="$2"
  local dockerfile=""
  local source_image=""
  if dockerfile="$(release_dockerfile_for "${service}")"; then
    DOCKER_BUILDKIT=1 docker build \
      --platform linux/amd64 \
      --no-cache \
      --file "${dockerfile}" \
      --tag "${target}" \
      .
  elif source_image="$(source_image_for "${service}")"; then
    docker pull --platform linux/amd64 "${source_image}"
    docker tag "${source_image}" "${target}"
  else
    test -f "services/${service}/Dockerfile"
    DOCKER_BUILDKIT=1 docker build \
      --platform linux/amd64 \
      --no-cache \
      --file "services/${service}/Dockerfile" \
      --tag "${target}" \
      .
  fi
}

release_step_scan() {
  trivy image \
    --cache-dir "${TRIVY_CACHE_DIR}" \
    --skip-db-update \
    --ignore-unfixed \
    --severity CRITICAL,HIGH \
    --exit-code 1 \
    --no-progress \
    --timeout 15m \
    "$1"
}

release_step_push() {
  docker push "$1"
}

release_step_resolve_digest() {
  local target="$1"
  local repository="${target%:*}"
  local resolved=""
  resolved="$(
    docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${target}" \
      | awk -v prefix="${repository}@" 'index($0, prefix) == 1 { print; exit }'
  )"
  [[ "${resolved}" == "${repository}@sha256:"* ]]
  [[ "${resolved#*@}" =~ ^sha256:[0-9a-f]{64}$ ]]
  printf '%s\n' "${resolved}"
}

release_step_resolve_portable_image_id() {
  local target="$1"
  local archive="${release_work_dir}/portable-image.tar"
  docker image save -o "${archive}" "${target}"
  PYTHONPATH="infra/ota-helper" python3 - "${archive}" <<'PY'
import sys
from pathlib import Path

from release_engine import docker_archive_config_digest

print(docker_archive_config_digest(Path(sys.argv[1])))
PY
  rm -f -- "${archive}"
}

release_step_sign_image() {
  cosign sign --yes --signing-config "${signing_config}" --key "${signing_key}" "$1"
}

release_step_verify_image() {
  cosign verify --insecure-ignore-tlog=true --key "${signing_pub}" "$1" >/dev/null
}

release_step_load_inherited_images() {
  PYTHONPATH="infra/ota-helper" python3 - \
    "${predecessor_path}" \
    "${predecessor_manifest_path}" \
    "${predecessor_signature_path}" \
    "${changed_services_path}" \
    "${inherited_images_path}" <<'PY'
import base64
import binascii
import hashlib
import json
import os
import sys
from pathlib import Path

from release_engine import canonical_manifest_bytes, validate_manifest

predecessor = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
changed = set(Path(sys.argv[4]).read_text(encoding="utf-8").splitlines())
service_inventory = tuple(os.environ["RELEASE_SERVICE_INVENTORY"].splitlines())
if predecessor is None:
    if changed != set(service_inventory):
        raise SystemExit("a partial release requires a completed predecessor")
    Path(sys.argv[5]).write_text("", encoding="utf-8")
    raise SystemExit(0)

manifest = validate_manifest(predecessor.get("manifest"))
manifest_bytes = canonical_manifest_bytes(manifest)
expected_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
if predecessor.get("manifest_digest") != expected_digest:
    raise SystemExit("predecessor manifest digest mismatch")
if manifest["signing_key_sha256"] != os.environ["SIGNING_KEY_SHA256"]:
    if changed != set(service_inventory):
        raise SystemExit("signing-key rotation requires rebuilding every service")
    Path(sys.argv[5]).write_text("", encoding="utf-8")
    raise SystemExit(0)
if (
    manifest["host_release_id"] != os.environ["HOST_RELEASE_ID"]
    or manifest["topology_digest"] != os.environ["TOPOLOGY_DIGEST"]
):
    raise SystemExit("predecessor topology binding changed")
try:
    signature = base64.b64decode(predecessor.get("manifest_signature", ""), validate=True)
except (binascii.Error, ValueError) as exc:
    raise SystemExit("predecessor manifest signature is invalid") from exc
if not signature:
    raise SystemExit("predecessor manifest signature is missing")
Path(sys.argv[2]).write_bytes(manifest_bytes)
Path(sys.argv[3]).write_bytes(signature)
Path(sys.argv[5]).write_text(
    "".join(
        f"{service}\t{manifest['services'][service]}\t{manifest['image_ids'][service]}\n"
        for service in service_inventory
        if service not in changed
    ),
    encoding="utf-8",
)
PY

  if [[ -s "${predecessor_manifest_path}" ]]; then
    cosign verify-blob \
      --key "${signing_pub}" \
      --insecure-ignore-tlog=true \
      --bundle "${predecessor_signature_path}" \
      "${predecessor_manifest_path}" >/dev/null
  fi
  while IFS=$'\t' read -r service inherited_ref inherited_id; do
    [[ -n "${service}" && -n "${inherited_ref}" && "${inherited_id}" =~ ^sha256:[0-9a-f]{64}$ ]]
    release_step_scan "${inherited_ref}"
    release_step_verify_image "${inherited_ref}"
    case "${service}" in
      ai-ctrl-optimizer|ai-softsensor|edge-api|edge-daq|edge-monitor|license-service)
        docker pull --platform linux/amd64 "${inherited_ref}"
        inherited_actual_id="$(release_step_resolve_portable_image_id "${inherited_ref}")"
        [[ "${inherited_actual_id}" == "${inherited_id}" ]]
        python3 scripts/ci/python_base_image_contract.py verify-image \
          --service "${service}" \
          --reference "${inherited_ref}" \
          --expected-image-id "${inherited_id}" \
          --output "${python_base_receipt_dir}/${service}.json"
        LOCAL_TARGETS+=("${inherited_ref}")
        ;;
    esac
    IMAGE_REFS["${service}"]="${inherited_ref}"
    IMAGE_IDS["${service}"]="${inherited_id}"
  done < "${inherited_images_path}"
}

release_step_sign_manifest() {
  cosign sign-blob \
    --yes \
    --signing-config "${signing_config}" \
    --key "${signing_key}" \
    --bundle "${manifest_signature}" \
    "${manifest_path}" >/dev/null
}

release_step_verify_manifest() {
  cosign verify-blob \
    --key "${signing_pub}" \
    --insecure-ignore-tlog=true \
    --bundle "${manifest_signature}" \
    "${manifest_path}" >/dev/null
}

release_step_complete_cloud() {
  python3 - "${release_metadata_path}" "${manifest_path}" "${manifest_signature}" <<'PY'
import base64
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

metadata = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
manifest_bytes = Path(sys.argv[2]).read_bytes()
manifest = json.loads(manifest_bytes)
signature = base64.b64encode(Path(sys.argv[3]).read_bytes()).decode()
body = json.dumps(
    {"manifest": manifest, "manifest_signature": signature},
    separators=(",", ":"),
).encode()
request = urllib.request.Request(
    os.environ["CLOUD_OPS_RELEASE_API_URL"].rstrip("/")
    + f"/api/v1/software-releases/{metadata['id']}/complete",
    data=body,
    headers={
        "Content-Type": "application/json",
        "X-API-Key": os.environ["CLOUD_OPS_RELEASE_API_KEY"],
    },
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    payload = json.loads(response.read(1024 * 1024))
expected_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
if payload.get("status") != "completed" or payload.get("manifest_digest") != expected_digest:
    raise SystemExit("Cloud did not complete the exact SoftwareRelease manifest")
output = os.environ.get("SOFTWARE_RELEASE_OUTPUT", "")
if output:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    output_path.chmod(0o600)
print(f"SoftwareRelease completed: id={metadata['id']} digest={expected_digest}")
PY
}

release_step_resolve_predecessor
release_step_resolve_changed_services
while IFS= read -r service; do
  [[ -n "${service}" ]]
  CHANGED_SERVICES["${service}"]=1
done < "${changed_services_path}"

release_step_create_cloud
release_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["id"])' "${release_metadata_path}")"
release_sequence="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["sequence"])' "${release_metadata_path}")"

release_step_load_inherited_images

for service in "${SERVICES[@]}"; do
  if [[ -z "${CHANGED_SERVICES[${service}]+present}" ]]; then
    [[ -n "${IMAGE_REFS[${service}]:-}" ]]
    continue
  fi
  target="${HARBOR_HOST}/${HARBOR_PROJECT}/${service}:${RELEASE_VERSION}"
  LOCAL_TARGETS+=("${target}")
  release_step_build_or_mirror "${service}" "${target}"
  release_step_scan "${target}"
  release_step_push "${target}"
  digest_ref="$(release_step_resolve_digest "${target}")"
  release_step_sign_image "${digest_ref}"
  release_step_verify_image "${digest_ref}"
  IMAGE_REFS["${service}"]="${digest_ref}"
  image_id="$(release_step_resolve_portable_image_id "${target}")"
  [[ "${image_id}" =~ ^sha256:[0-9a-f]{64}$ ]]
  case "${service}" in
    ai-ctrl-optimizer|ai-softsensor|edge-api|edge-daq|edge-monitor|license-service)
      python3 scripts/ci/python_base_image_contract.py verify-image \
        --service "${service}" \
        --reference "${target}" \
        --expected-image-id "${image_id}" \
        --output "${python_base_receipt_dir}/${service}.json"
      ;;
  esac
  IMAGE_IDS["${service}"]="${image_id}"
done

: > "${image_map_path}"
for service in "${SERVICES[@]}"; do
  printf '%s\t%s\t%s\n' \
    "${service}" \
    "${IMAGE_REFS[${service}]}" \
    "${IMAGE_IDS[${service}]}" >> "${image_map_path}"
done

export RELEASE_ID="${release_id}"
export RELEASE_SEQUENCE="${release_sequence}"
PYTHONPATH="infra/ota-helper" python3 - "${image_map_path}" "${manifest_path}" <<'PY'
import os
import sys
from pathlib import Path

from release_engine import COSIGN_TOOLCHAIN, canonical_manifest_bytes, validate_manifest

services = {}
image_ids = {}
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    service, reference, image_id = line.split("\t")
    services[service] = reference
    image_ids[service] = image_id
manifest = {
    "schema": "drycontroller.software-release.v1",
    "release_id": os.environ["RELEASE_ID"],
    "host_release_id": os.environ["HOST_RELEASE_ID"],
    "topology_digest": os.environ["TOPOLOGY_DIGEST"],
    "sequence": int(os.environ["RELEASE_SEQUENCE"]),
    "version": os.environ["RELEASE_VERSION"],
    "source_commit": os.environ["RELEASE_SOURCE_COMMIT"],
    "registry": os.environ["HARBOR_HOST"],
    "signing_key_sha256": os.environ["SIGNING_KEY_SHA256"],
    "cosign": COSIGN_TOOLCHAIN,
    "services": services,
    "image_ids": image_ids,
}
validate_manifest(manifest)
Path(sys.argv[2]).write_bytes(canonical_manifest_bytes(manifest))
PY
chmod 600 "${manifest_path}"
python3 scripts/ci/python_base_image_contract.py verify-receipts \
  --directory "${python_base_receipt_dir}" \
  --manifest "${manifest_path}" \
  --output "${python_base_receipt_path}"
cat "${python_base_receipt_path}"
release_step_sign_manifest
release_step_verify_manifest
release_step_complete_cloud
