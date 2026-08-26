#!/usr/bin/env bash
# Fresh, build/sign-only RB-8 Gate C lane for the Gate B frozen Edge source.
set -Eeuo pipefail

: "${GATE_C_CONTROLLER_ROOT:?missing GATE_C_CONTROLLER_ROOT}"
: "${GATE_C_SOURCE_ROOT:?missing GATE_C_SOURCE_ROOT}"
: "${GATE_C_EVIDENCE_DIR:?missing GATE_C_EVIDENCE_DIR}"
: "${GATE_C_RUN_ID:?missing GATE_C_RUN_ID}"
: "${HARBOR_HOST:?missing HARBOR_HOST}"
: "${HARBOR_USER:?missing HARBOR_USER}"
: "${HARBOR_PASSWORD:?missing HARBOR_PASSWORD}"
: "${HOST_COSIGN_PRIVATE_KEY:?missing HOST_COSIGN_PRIVATE_KEY}"
: "${HOST_COSIGN_PASSWORD:?missing HOST_COSIGN_PASSWORD}"
: "${SOFTWARE_COSIGN_PRIVATE_KEY:?missing SOFTWARE_COSIGN_PRIVATE_KEY}"
: "${SOFTWARE_COSIGN_PASSWORD:?missing SOFTWARE_COSIGN_PASSWORD}"
: "${GATE_C_RECEIPT_COSIGN_PRIVATE_KEY:?missing GATE_C_RECEIPT_COSIGN_PRIVATE_KEY}"
: "${GATE_C_RECEIPT_COSIGN_PASSWORD:?missing GATE_C_RECEIPT_COSIGN_PASSWORD}"

readonly HELPER="${GATE_C_CONTROLLER_ROOT}/scripts/ci/gate_c_receipt.py"
readonly OFFLINE_SIGNING_CONFIG='{"mediaType":"application/vnd.dev.sigstore.signingconfig.v0.2+json","rekorTlogConfig":{},"tsaConfig":{}}'
readonly MANIFEST="${GATE_C_EVIDENCE_DIR}/inputs/integration-manifest.json"
readonly APPROVAL="${GATE_C_EVIDENCE_DIR}/inputs/gate-c-approval.json"
readonly CLEANUP_EVIDENCE="${GATE_C_EVIDENCE_DIR}/cleanup.json"
readonly TRIVY_BINARY="/usr/local/bin/trivy"

[[ "${GATE_C_RUN_ID}" =~ ^[a-z0-9][a-z0-9._-]{7,63}$ ]]
test -f "${HELPER}"
test -f "${MANIFEST}"
test -f "${APPROVAL}"
test ! -L "${GATE_C_EVIDENCE_DIR}"
test ! -e "${GATE_C_EVIDENCE_DIR}/gate-c-receipt.json"
test ! -e "${GATE_C_EVIDENCE_DIR}/failure.json"
for command in cosign curl docker git python3 sha256sum tar; do
  command -v "${command}" >/dev/null
done
bash "${GATE_C_CONTROLLER_ROOT}/scripts/ci/verify-cosign-toolchain.sh"
python3 "${GATE_C_CONTROLLER_ROOT}/scripts/ci/verify-trivy-executable.py" >/dev/null

work_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/drycontroller-edge-gate-c.${GATE_C_RUN_ID}.XXXXXX")"
chmod 700 "${work_dir}"
export DOCKER_CONFIG="${work_dir}/docker"
mkdir -p "${DOCKER_CONFIG}"
chmod 700 "${DOCKER_CONFIG}"
builder_name="dc-gc-$(printf '%s' "${GATE_C_RUN_ID}" | sha256sum | cut -c1-32)"
failure_step="source_identity"
success=0
builder_created=0
declare -a LOCAL_IMAGES=()

mkdir -p \
  "${GATE_C_EVIDENCE_DIR}/base-images" \
  "${GATE_C_EVIDENCE_DIR}/keys" \
  "${GATE_C_EVIDENCE_DIR}/scans" \
  "${GATE_C_EVIDENCE_DIR}/signatures"
chmod 700 \
  "${GATE_C_EVIDENCE_DIR}/base-images" \
  "${GATE_C_EVIDENCE_DIR}/keys" \
  "${GATE_C_EVIDENCE_DIR}/scans" \
  "${GATE_C_EVIDENCE_DIR}/signatures"
: > "${GATE_C_EVIDENCE_DIR}/artifacts.tsv"
chmod 600 "${GATE_C_EVIDENCE_DIR}/artifacts.tsv"

write_cleanup_evidence() {
  local builder_absent="$1"
  local containers_absent="$2"
  local credentials_absent="$3"
  local local_images_absent="$4"
  local temporary_archives_absent="$5"
  local work_namespace_absent="$6"
  python3 - "${CLEANUP_EVIDENCE}" \
    "${builder_absent}" "${containers_absent}" "${credentials_absent}" \
    "${local_images_absent}" "${temporary_archives_absent}" \
    "${work_namespace_absent}" <<'PY'
import json
import sys
from pathlib import Path

names = (
    "builder_absent",
    "containers_absent",
    "credentials_absent",
    "local_images_absent",
    "temporary_archives_absent",
    "work_namespace_absent",
)
payload = {name: value == "true" for name, value in zip(names, sys.argv[2:], strict=True)}
path = Path(sys.argv[1])
if path.exists():
    path.unlink()
path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
path.chmod(0o600)
PY
}

cleanup_resources() {
  set +e
  local builder_absent=true
  local containers_absent=true
  local credentials_absent=true
  local local_images_absent=true
  local temporary_archives_absent=true
  local work_namespace_absent=true

  if [[ "${builder_created}" -eq 1 ]]; then
    docker buildx rm "${builder_name}" >/dev/null 2>&1
  fi
  for image in "${LOCAL_IMAGES[@]:-}"; do
    [[ -n "${image}" ]] || continue
    docker image rm -- "${image}" >/dev/null 2>&1
  done
  rm -rf -- "${work_dir}"

  if docker info >/dev/null 2>&1; then
    docker buildx inspect "${builder_name}" >/dev/null 2>&1 && builder_absent=false
    if docker ps -a --format '{{.Names}}' | grep -Fx "buildx_buildkit_${builder_name}0" >/dev/null; then
      containers_absent=false
    fi
    for image in "${LOCAL_IMAGES[@]:-}"; do
      [[ -n "${image}" ]] || continue
      docker image inspect "${image}" >/dev/null 2>&1 && local_images_absent=false
    done
  else
    builder_absent=false
    containers_absent=false
    local_images_absent=false
  fi
  [[ ! -e "${DOCKER_CONFIG}/config.json" ]] || credentials_absent=false
  [[ ! -e "${work_dir}/portable-image.tar" ]] || temporary_archives_absent=false
  [[ ! -e "${work_dir}/host-bundle.tar" ]] || temporary_archives_absent=false
  [[ ! -e "${work_dir}" ]] || work_namespace_absent=false
  write_cleanup_evidence \
    "${builder_absent}" "${containers_absent}" "${credentials_absent}" \
    "${local_images_absent}" "${temporary_archives_absent}" \
    "${work_namespace_absent}"
  set -e
  [[ "${builder_absent}" == true ]]
  [[ "${containers_absent}" == true ]]
  [[ "${credentials_absent}" == true ]]
  [[ "${local_images_absent}" == true ]]
  [[ "${temporary_archives_absent}" == true ]]
  [[ "${work_namespace_absent}" == true ]]
}

on_exit() {
  local rc="$?"
  trap - EXIT
  if [[ "${success}" -ne 1 ]]; then
    cleanup_resources || true
    python3 "${HELPER}" failure \
      --evidence-dir "${GATE_C_EVIDENCE_DIR}" \
      --step "${failure_step}" \
      --exit-code "${rc}" \
      --cleanup "${CLEANUP_EVIDENCE}" || true
  fi
  exit "${rc}"
}
trap on_exit EXIT
trap 'exit 130' INT TERM HUP

python3 "${HELPER}" verify-source \
  --source-root "${GATE_C_SOURCE_ROOT}" \
  --manifest "${MANIFEST}" \
  --output "${GATE_C_EVIDENCE_DIR}/source-identity.json"
python3 "${GATE_C_SOURCE_ROOT}/scripts/ci/python_base_image_contract.py" \
  verify-dockerfiles --root "${GATE_C_SOURCE_ROOT}" >/dev/null

failure_step="public_key_identity"
COSIGN_PASSWORD="${HOST_COSIGN_PASSWORD}" cosign public-key \
  --key env://HOST_COSIGN_PRIVATE_KEY > "${GATE_C_EVIDENCE_DIR}/keys/host-cosign.pub"
COSIGN_PASSWORD="${SOFTWARE_COSIGN_PASSWORD}" cosign public-key \
  --key env://SOFTWARE_COSIGN_PRIVATE_KEY > "${GATE_C_EVIDENCE_DIR}/keys/software-cosign.pub"
COSIGN_PASSWORD="${GATE_C_RECEIPT_COSIGN_PASSWORD}" cosign public-key \
  --key env://GATE_C_RECEIPT_COSIGN_PRIVATE_KEY > "${GATE_C_EVIDENCE_DIR}/keys/receipt-cosign.pub"
chmod 600 "${GATE_C_EVIDENCE_DIR}/keys/"*.pub
python3 "${HELPER}" verify-keys \
  --evidence-dir "${GATE_C_EVIDENCE_DIR}" \
  --approval "${APPROVAL}"

registry_namespace="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["registry"]["namespace"])' "${APPROVAL}")"
buildkit_image="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["toolchain"]["buildkit_image"])' "${APPROVAL}")"
trivy_db_url="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["toolchain"]["trivy_db"]["package_url"])' "${APPROVAL}")"
trivy_db_sha256="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["toolchain"]["trivy_db"]["package_sha256"])' "${APPROVAL}")"

failure_step="registry_auth"
python3 "${GATE_C_SOURCE_ROOT}/scripts/ci/software_release_builder.py" \
  write-docker-auth "${DOCKER_CONFIG}/config.json"

failure_step="fresh_buildkit"
if docker buildx inspect "${builder_name}" >/dev/null 2>&1; then
  echo "Gate C builder namespace already exists; resume is forbidden" >&2
  exit 1
fi
docker buildx create \
  --name "${builder_name}" \
  --driver docker-container \
  --driver-opt "image=${buildkit_image}" \
  --use >/dev/null
builder_created=1
LOCAL_IMAGES+=("${buildkit_image}")
docker buildx inspect "${builder_name}" --bootstrap > "${work_dir}/buildkit-inspect.txt"
buildkit_version="$(awk '
  $1 == "BuildKit:" {print $2; exit}
  $1 == "BuildKit" && $2 == "version:" {print $3; exit}
' "${work_dir}/buildkit-inspect.txt")"
[[ "${buildkit_version}" == v* ]]

failure_step="fresh_trivy_db"
trivy_cache="${work_dir}/trivy"
trivy_package="${work_dir}/trivy-db.tar.gz"
mkdir -p "${trivy_cache}"
curl -fsSL --retry 3 --retry-delay 2 -o "${trivy_package}" "${trivy_db_url}"
test "$(sha256sum "${trivy_package}" | awk '{print $1}')" = "${trivy_db_sha256}"
tar -xzf "${trivy_package}" -C "${trivy_cache}"
test -f "${trivy_cache}/db/trivy.db"
test -f "${trivy_cache}/db/metadata.json"
trivy_metadata_sha256="$(sha256sum "${trivy_cache}/db/metadata.json" | awk '{print $1}')"
python3 "${HELPER}" verify-toolchain \
  --approval "${APPROVAL}" \
  --buildkit-version "${buildkit_version}" \
  --trivy-db-metadata-sha256 "${trivy_metadata_sha256}"

mapfile -t SERVICES < <(
  python3 "${GATE_C_SOURCE_ROOT}/scripts/ci/software_release_builder.py" inventory
)

source_image_for() {
  python3 "${GATE_C_SOURCE_ROOT}/scripts/ci/software_release_builder.py" \
    source-image "$1" 2>/dev/null
}

release_dockerfile_for() {
  python3 "${GATE_C_SOURCE_ROOT}/scripts/ci/software_release_builder.py" \
    release-dockerfile "$1" 2>/dev/null
}

assert_fresh_remote_target() {
  if docker manifest inspect "$1" >/dev/null 2>&1; then
    echo "Gate C target already exists; predecessor/resume is forbidden: $1" >&2
    return 1
  fi
}

portable_image_id() {
  local target="$1"
  local archive="${work_dir}/portable-image.tar"
  test ! -e "${archive}"
  docker image save -o "${archive}" "${target}"
  python3 "${GATE_C_SOURCE_ROOT}/scripts/ci/software_release_builder.py" \
    portable-image-id "${archive}"
  rm -f -- "${archive}"
}

resolve_digest_reference() {
  local target="$1"
  local repository="${target%:*}"
  local resolved
  resolved="$(
    docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${target}" \
      | awk -v prefix="${repository}@" 'index($0, prefix) == 1 {print; exit}'
  )"
  [[ "${resolved}" == "${repository}@sha256:"* ]]
  printf '%s\n' "${resolved}"
}

scan_image() {
  local target="$1"
  local output="$2"
  "${TRIVY_BINARY}" image \
    --cache-dir "${trivy_cache}" \
    --skip-db-update \
    --ignore-unfixed \
    --severity CRITICAL,HIGH \
    --exit-code 1 \
    --format json \
    --output "${output}" \
    --no-progress \
    --timeout 15m \
    "${target}"
}

sign_and_record() {
  local role="$1"
  local name="$2"
  local target="$3"
  local image_id="$4"
  local scan_path="$5"
  local base_evidence="${6:-}"
  local key_env key_password public_key
  local scan_relative verification_relative base_relative
  if [[ "${role}" == host ]]; then
    key_env="HOST_COSIGN_PRIVATE_KEY"
    key_password="${HOST_COSIGN_PASSWORD}"
    public_key="${GATE_C_EVIDENCE_DIR}/keys/host-cosign.pub"
  else
    key_env="SOFTWARE_COSIGN_PRIVATE_KEY"
    key_password="${SOFTWARE_COSIGN_PASSWORD}"
    public_key="${GATE_C_EVIDENCE_DIR}/keys/software-cosign.pub"
  fi
  docker push "${target}" >/dev/null
  local digest_ref
  digest_ref="$(resolve_digest_reference "${target}")"
  COSIGN_PASSWORD="${key_password}" cosign sign \
    --yes \
    --signing-config <(printf '%s' "${OFFLINE_SIGNING_CONFIG}") \
    --key "env://${key_env}" \
    "${digest_ref}"
  local verification="${GATE_C_EVIDENCE_DIR}/signatures/${name}.cosign-verify.json"
  cosign verify \
    --insecure-ignore-tlog=true \
    --key "${public_key}" \
    --output json \
    "${digest_ref}" > "${verification}"
  scan_relative="scans/${scan_path##*/}"
  verification_relative="signatures/${verification##*/}"
  base_relative="-"
  if [[ -n "${base_evidence}" ]]; then
    base_relative="base-images/${base_evidence##*/}"
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "${role}" "${name}" "${digest_ref}" "${image_id}" \
    "${scan_relative}" "${verification_relative}" "${base_relative}" \
    >> "${GATE_C_EVIDENCE_DIR}/artifacts.tsv"
}

for service in "${SERVICES[@]}"; do
  failure_step="software_${service}_fresh_target"
  target="${HARBOR_HOST}/${registry_namespace}/${service}:gate-c"
  assert_fresh_remote_target "${target}"
  if docker image inspect "${target}" >/dev/null 2>&1; then
    echo "Gate C local target already exists; reuse is forbidden: ${target}" >&2
    exit 1
  fi
  LOCAL_IMAGES+=("${target}")
  failure_step="software_${service}_build"
  if dockerfile="$(release_dockerfile_for "${service}")"; then
    docker buildx build \
      --builder "${builder_name}" \
      --platform linux/amd64 \
      --no-cache \
      --pull \
      --load \
      --file "${GATE_C_SOURCE_ROOT}/${dockerfile}" \
      --tag "${target}" \
      "${GATE_C_SOURCE_ROOT}"
  elif source_image="$(source_image_for "${service}")"; then
    docker image rm -- "${source_image}" >/dev/null 2>&1 || true
    LOCAL_IMAGES+=("${source_image}")
    docker pull --platform linux/amd64 "${source_image}"
    docker tag "${source_image}" "${target}"
  else
    test -f "${GATE_C_SOURCE_ROOT}/services/${service}/Dockerfile"
    docker buildx build \
      --builder "${builder_name}" \
      --platform linux/amd64 \
      --no-cache \
      --pull \
      --load \
      --file "${GATE_C_SOURCE_ROOT}/services/${service}/Dockerfile" \
      --tag "${target}" \
      "${GATE_C_SOURCE_ROOT}"
  fi
  failure_step="software_${service}_scan"
  scan_path="${GATE_C_EVIDENCE_DIR}/scans/${service}.trivy.json"
  scan_image "${target}" "${scan_path}"
  image_id="$(portable_image_id "${target}")"
  [[ "${image_id}" =~ ^sha256:[0-9a-f]{64}$ ]]
  base_evidence=""
  case "${service}" in
    ai-ctrl-optimizer|ai-softsensor|edge-api|edge-daq|edge-monitor|license-service)
      base_evidence="${GATE_C_EVIDENCE_DIR}/base-images/${service}.json"
      python3 "${GATE_C_SOURCE_ROOT}/scripts/ci/python_base_image_contract.py" \
        verify-image \
        --service "${service}" \
        --reference "${target}" \
        --expected-image-id "${image_id}" \
        --output "${base_evidence}"
      ;;
  esac
  failure_step="software_${service}_push_sign"
  sign_and_record \
    software "${service}" "${target}" "${image_id}" "${scan_path}" "${base_evidence}"
done

failure_step="host_stage"
bundle_parent="${work_dir}/bundle"
bundle_root="${bundle_parent}/host-release"
topology_work="${work_dir}/topology.json"
python3 "${GATE_C_SOURCE_ROOT}/scripts/prepare-host-release.py" stage \
  --source-root "${GATE_C_SOURCE_ROOT}" \
  --output-root "${bundle_root}" \
  --topology-output "${topology_work}" >/dev/null
cp "${topology_work}" "${GATE_C_EVIDENCE_DIR}/topology.json"
tar \
  --sort=name \
  --mtime='UTC 1970-01-01' \
  --owner=0 \
  --group=0 \
  --numeric-owner \
  --format=posix \
  -C "${bundle_parent}" \
  -cf "${work_dir}/host-bundle.tar" \
  host-release
host_target="${HARBOR_HOST}/${registry_namespace}/host-bundle:gate-c"
failure_step="host_fresh_target"
assert_fresh_remote_target "${host_target}"
if docker image inspect "${host_target}" >/dev/null 2>&1; then
  echo "Gate C local host target already exists; reuse is forbidden" >&2
  exit 1
fi
LOCAL_IMAGES+=("${host_target}")
failure_step="host_build_scan"
docker import --platform linux/amd64 "${work_dir}/host-bundle.tar" "${host_target}" >/dev/null
host_scan="${GATE_C_EVIDENCE_DIR}/scans/host-bundle.trivy.json"
scan_image "${host_target}" "${host_scan}"
host_image_id="$(portable_image_id "${host_target}")"
[[ "${host_image_id}" =~ ^sha256:[0-9a-f]{64}$ ]]
failure_step="host_push_sign"
sign_and_record host host-bundle "${host_target}" "${host_image_id}" "${host_scan}" ""

failure_step="cleanup"
cleanup_resources

failure_step="canonical_receipt"
receipt_path="${GATE_C_EVIDENCE_DIR}/gate-c-receipt.json"
receipt_bundle="${GATE_C_EVIDENCE_DIR}/gate-c-receipt.sigstore.json"
python3 "${HELPER}" receipt \
  --evidence-dir "${GATE_C_EVIDENCE_DIR}" \
  --source-root "${GATE_C_SOURCE_ROOT}" \
  --buildkit-version "${buildkit_version}" \
  --trivy-db-metadata-sha256 "${trivy_metadata_sha256}" \
  --output "${receipt_path}"
COSIGN_PASSWORD="${GATE_C_RECEIPT_COSIGN_PASSWORD}" cosign sign-blob \
  --yes \
  --signing-config <(printf '%s' "${OFFLINE_SIGNING_CONFIG}") \
  --key env://GATE_C_RECEIPT_COSIGN_PRIVATE_KEY \
  --bundle "${receipt_bundle}" \
  "${receipt_path}" >/dev/null
cosign verify-blob \
  --insecure-ignore-tlog=true \
  --key "${GATE_C_EVIDENCE_DIR}/keys/receipt-cosign.pub" \
  --bundle "${receipt_bundle}" \
  "${receipt_path}" > "${GATE_C_EVIDENCE_DIR}/gate-c-receipt.verify.txt"
success=1
