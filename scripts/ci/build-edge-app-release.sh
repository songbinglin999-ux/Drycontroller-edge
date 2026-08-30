#!/usr/bin/env bash
# Build, sign, and complete one immutable edge-app-v1 release.
set -euo pipefail

: "${RELEASE_VERSION:?missing RELEASE_VERSION}"
: "${RELEASE_SOURCE_COMMIT:?missing RELEASE_SOURCE_COMMIT}"
: "${EDGE_APP_APPROVED_DB_BASELINE_COMMIT:?missing EDGE_APP_APPROVED_DB_BASELINE_COMMIT}"
: "${HARBOR_HOST:?missing HARBOR_HOST}"
: "${HARBOR_PROJECT:?missing HARBOR_PROJECT}"
: "${HARBOR_USER:?missing HARBOR_USER}"
: "${HARBOR_PASSWORD:?missing HARBOR_PASSWORD}"
: "${COSIGN_PRIVATE_KEY:?missing COSIGN_PRIVATE_KEY}"
: "${COSIGN_PASSWORD:?missing COSIGN_PASSWORD}"
: "${COSIGN_PUBLIC_KEY_SHA256:?missing COSIGN_PUBLIC_KEY_SHA256}"
: "${CLOUD_OPS_RELEASE_API_URL:?missing CLOUD_OPS_RELEASE_API_URL}"
: "${CLOUD_OPS_RELEASE_API_KEY:?missing CLOUD_OPS_RELEASE_API_KEY}"
: "${TRIVY_CACHE_DIR:?missing TRIVY_CACHE_DIR}"

[[ "${RELEASE_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$ ]]
[[ "${RELEASE_SOURCE_COMMIT}" =~ ^[0-9a-f]{40}$ ]]
[[ "${EDGE_APP_APPROVED_DB_BASELINE_COMMIT}" =~ ^[0-9a-f]{40}$ ]]
[[ "${CLOUD_OPS_RELEASE_API_URL}" == https://* ]]
[[ "${COSIGN_PUBLIC_KEY_SHA256}" =~ ^[0-9a-f]{64}$ ]]

readonly DOCKER_BINARY="/usr/bin/docker"
readonly COSIGN_BINARY="/usr/local/bin/cosign"
readonly TRIVY_BINARY="/usr/local/bin/trivy"
readonly PYTHON_BINARY="/usr/bin/python3"
"${PYTHON_BINARY}" scripts/ci/verify-edge-app-migration-scope.py \
  --approved-baseline "${EDGE_APP_APPROVED_DB_BASELINE_COMMIT}" \
  --source "${RELEASE_SOURCE_COMMIT}"
"${PYTHON_BINARY}" scripts/ci/verify-edge-app-toolchain.py >/dev/null

release_dir="$(/usr/bin/mktemp -d "${RUNNER_TEMP:-/tmp}/drycontroller-edge-app.XXXXXX")"
/usr/bin/chmod 700 "${release_dir}"
trap '/usr/bin/rm -rf -- "${release_dir}"' EXIT
export DOCKER_CONFIG="${release_dir}/docker"
/usr/bin/mkdir -m 700 "${DOCKER_CONFIG}"
printf '{"auths":{"%s":{"auth":"%s"}}}' \
  "${HARBOR_HOST}" \
  "$(printf '%s' "${HARBOR_USER}:${HARBOR_PASSWORD}" | /usr/bin/base64 | /usr/bin/tr -d '\n')" \
  > "${DOCKER_CONFIG}/config.json"
/usr/bin/chmod 600 "${DOCKER_CONFIG}/config.json"

signing_key="${release_dir}/cosign.key"
signing_pub="${release_dir}/cosign.pub"
signing_config="${release_dir}/cosign-signing-config.json"
metadata="${release_dir}/release.json"
images="${release_dir}/images.tsv"
manifest="${release_dir}/manifest.json"
bundle="${release_dir}/manifest.sigstore.json"
printf '%s' "${COSIGN_PRIVATE_KEY}" > "${signing_key}"
/usr/bin/chmod 600 "${signing_key}"
"${COSIGN_BINARY}" public-key --key "${signing_key}" > "${signing_pub}"
[[ "$(/usr/bin/sha256sum "${signing_pub}" | /usr/bin/awk '{print $1}')" == "${COSIGN_PUBLIC_KEY_SHA256}" ]] \
  || { echo "cosign public key fingerprint is not approved" >&2; exit 1; }
"${COSIGN_BINARY}" signing-config create \
  --no-default-fulcio --no-default-oidc --no-default-rekor --no-default-tsa \
  --out "${signing_config}"

"${PYTHON_BINARY}" scripts/ci/edge_app_release_builder.py create "${metadata}"
: > "${images}"
while IFS= read -r service; do
  target="${HARBOR_HOST}/${HARBOR_PROJECT}/${service}:${RELEASE_VERSION}"
  "${DOCKER_BINARY}" build --platform linux/amd64 --no-cache \
    --file "services/${service}/Dockerfile" --tag "${target}" .
  "${TRIVY_BINARY}" image \
    --cache-dir "${TRIVY_CACHE_DIR}" --skip-db-update --ignore-unfixed \
    --severity CRITICAL,HIGH --exit-code 1 --no-progress --timeout 15m "${target}"
  "${DOCKER_BINARY}" push "${target}"
  repository="${target%:*}"
  reference="$("${DOCKER_BINARY}" image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${target}" \
    | /usr/bin/awk -v prefix="${repository}@" 'index($0,prefix)==1 {print; exit}')"
  [[ "${reference}" =~ @sha256:[0-9a-f]{64}$ ]]
  "${COSIGN_BINARY}" sign --yes --signing-config "${signing_config}" --key "${signing_key}" "${reference}"
  "${COSIGN_BINARY}" verify --insecure-ignore-tlog=true --key "${signing_pub}" "${reference}" >/dev/null
  printf '%s\t%s\n' "${service}" "${reference}" >> "${images}"
done < <("${PYTHON_BINARY}" scripts/ci/edge_app_release_builder.py inventory)

"${PYTHON_BINARY}" scripts/ci/edge_app_release_builder.py manifest "${metadata}" "${images}" "${manifest}"
"${COSIGN_BINARY}" sign-blob --yes --signing-config "${signing_config}" --key "${signing_key}" \
  --bundle "${bundle}" "${manifest}" >/dev/null
"${PYTHON_BINARY}" scripts/ci/edge_app_release_builder.py complete "${manifest}" "${bundle}"
