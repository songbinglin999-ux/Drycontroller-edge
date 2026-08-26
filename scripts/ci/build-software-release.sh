#!/usr/bin/env bash
# Build one complete, signed Edge SoftwareRelease and complete it in Cloud last.
set -euo pipefail

TRIVY_BINARY="/usr/local/bin/trivy"
readonly TRIVY_BINARY

mapfile -t SERVICES < <(python3 scripts/ci/software_release_builder.py inventory)

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
command -v cosign >/dev/null
command -v python3 >/dev/null
bash scripts/ci/verify-cosign-toolchain.sh
python3 scripts/ci/verify-trivy-executable.py
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
resumable_release_path="${release_work_dir}/resumable-release.json"
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

python3 scripts/ci/software_release_builder.py write-docker-auth "${DOCKER_CONFIG}/config.json"
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
  python3 scripts/ci/software_release_builder.py resolve-predecessor \
    "${predecessor_path}" \
    "${resumable_release_path}"
}

release_step_resolve_changed_services() {
  python3 scripts/ci/software_release_builder.py resolve-changed-services \
    "${predecessor_path}" "${changed_services_path}"
}

release_step_create_cloud() {
  python3 scripts/ci/software_release_builder.py create-cloud "${release_metadata_path}"
}

source_image_for() {
  case "$1" in
    alertmanager|mosquitto|mosquitto-init|prometheus)
      python3 scripts/ci/software_release_builder.py source-image "$1"
      ;;
    *) return 1 ;;
  esac
}

release_dockerfile_for() {
  case "$1" in
    influxdb|postgres)
      python3 scripts/ci/software_release_builder.py release-dockerfile "$1"
      ;;
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
  python3 scripts/ci/verify-trivy-executable.py
  "${TRIVY_BINARY}" image \
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
  python3 scripts/ci/software_release_builder.py portable-image-id "${archive}"
  rm -f -- "${archive}"
}

release_step_sign_image() {
  cosign sign --yes --signing-config "${signing_config}" --key "${signing_key}" "$1"
}

release_step_verify_image() {
  cosign verify --insecure-ignore-tlog=true --key "${signing_pub}" "$1" >/dev/null
}

release_step_load_inherited_images() {
  python3 scripts/ci/software_release_builder.py load-inherited \
    "${predecessor_path}" \
    "${predecessor_manifest_path}" \
    "${predecessor_signature_path}" \
    "${changed_services_path}" \
    "${inherited_images_path}"

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

release_step_resume_completed_release() {
  python3 scripts/ci/software_release_builder.py resume \
    "${resumable_release_path}" \
    "${manifest_path}" \
    "${manifest_signature}" \
    "${inherited_images_path}"
  chmod 600 "${manifest_path}" "${manifest_signature}"
  cosign verify-blob \
    --key "${signing_pub}" \
    --insecure-ignore-tlog=true \
    --bundle "${manifest_signature}" \
    "${manifest_path}" >/dev/null
  while IFS=$'\t' read -r service resumed_ref resumed_id; do
    [[ -n "${service}" && -n "${resumed_ref}" && "${resumed_id}" =~ ^sha256:[0-9a-f]{64}$ ]]
    release_step_scan "${resumed_ref}"
    release_step_verify_image "${resumed_ref}"
    docker pull --platform linux/amd64 "${resumed_ref}"
    resumed_actual_id="$(release_step_resolve_portable_image_id "${resumed_ref}")"
    [[ "${resumed_actual_id}" == "${resumed_id}" ]]
    LOCAL_TARGETS+=("${resumed_ref}")
    case "${service}" in
      ai-ctrl-optimizer|ai-softsensor|edge-api|edge-daq|edge-monitor|license-service)
        python3 scripts/ci/python_base_image_contract.py verify-image \
          --service "${service}" \
          --reference "${resumed_ref}" \
          --expected-image-id "${resumed_id}" \
          --output "${python_base_receipt_dir}/${service}.json"
        ;;
    esac
  done < "${inherited_images_path}"
  python3 scripts/ci/python_base_image_contract.py verify-receipts \
    --directory "${python_base_receipt_dir}" \
    --manifest "${manifest_path}" \
    --output "${python_base_receipt_path}"
  python3 scripts/ci/software_release_builder.py write-release-output \
    "${resumable_release_path}"
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
  python3 scripts/ci/software_release_builder.py complete-cloud \
    "${release_metadata_path}" \
    "${manifest_path}" \
    "${manifest_signature}"
}

release_step_resolve_predecessor
if [[ -s "${resumable_release_path}" ]]; then
  release_step_resume_completed_release
  exit 0
fi
release_step_resolve_changed_services
while IFS= read -r service; do
  [[ -n "${service}" ]]
  CHANGED_SERVICES["${service}"]=1
done < "${changed_services_path}"

release_step_create_cloud
release_id="$(python3 scripts/ci/software_release_builder.py field "${release_metadata_path}" id)"
release_sequence="$(python3 scripts/ci/software_release_builder.py field "${release_metadata_path}" sequence)"

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
  release_step_push "${target}"
  digest_ref="$(release_step_resolve_digest "${target}")"
  release_step_sign_image "${digest_ref}"
  release_step_verify_image "${digest_ref}"
  IMAGE_REFS["${service}"]="${digest_ref}"
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
python3 scripts/ci/software_release_builder.py create-manifest \
  "${image_map_path}" "${manifest_path}"
chmod 600 "${manifest_path}"
python3 scripts/ci/python_base_image_contract.py verify-receipts \
  --directory "${python_base_receipt_dir}" \
  --manifest "${manifest_path}" \
  --output "${python_base_receipt_path}"
cat "${python_base_receipt_path}"
release_step_sign_manifest
release_step_verify_manifest
release_step_complete_cloud
