#!/usr/bin/env bash
# Build, sign, and complete one immutable edge-app-v1 release.
set -euo pipefail

: "${RELEASE_VERSION:?missing RELEASE_VERSION}"
: "${RELEASE_SOURCE_COMMIT:?missing RELEASE_SOURCE_COMMIT}"
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
[[ "${CLOUD_OPS_RELEASE_API_URL}" == https://* ]]

release_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/drycontroller-edge-app.XXXXXX")"
chmod 700 "${release_dir}"
trap 'rm -rf -- "${release_dir}"' EXIT
export DOCKER_CONFIG="${release_dir}/docker"
mkdir -m 700 "${DOCKER_CONFIG}"
printf '{"auths":{"%s":{"auth":"%s"}}}' \
  "${HARBOR_HOST}" \
  "$(printf '%s' "${HARBOR_USER}:${HARBOR_PASSWORD}" | base64 | tr -d '\n')" \
  > "${DOCKER_CONFIG}/config.json"
chmod 600 "${DOCKER_CONFIG}/config.json"

signing_key="${release_dir}/cosign.key"
signing_pub="${release_dir}/cosign.pub"
signing_config="${release_dir}/cosign-signing-config.json"
metadata="${release_dir}/release.json"
images="${release_dir}/images.tsv"
manifest="${release_dir}/manifest.json"
bundle="${release_dir}/manifest.sigstore.json"
printf '%s' "${COSIGN_PRIVATE_KEY}" > "${signing_key}"
chmod 600 "${signing_key}"
cosign public-key --key "${signing_key}" > "${signing_pub}"
cosign signing-config create \
  --no-default-fulcio --no-default-oidc --no-default-rekor --no-default-tsa \
  --out "${signing_config}"

python3 scripts/ci/edge_app_release_builder.py create "${metadata}"
: > "${images}"
while IFS= read -r service; do
  target="${HARBOR_HOST}/${HARBOR_PROJECT}/${service}:${RELEASE_VERSION}"
  docker build --platform linux/amd64 --no-cache \
    --file "services/${service}/Dockerfile" --tag "${target}" .
  /usr/local/bin/trivy image \
    --cache-dir "${TRIVY_CACHE_DIR}" --skip-db-update --ignore-unfixed \
    --severity CRITICAL,HIGH --exit-code 1 --no-progress --timeout 15m "${target}"
  docker push "${target}"
  repository="${target%:*}"
  reference="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${target}" \
    | awk -v prefix="${repository}@" 'index($0,prefix)==1 {print; exit}')"
  [[ "${reference}" =~ @sha256:[0-9a-f]{64}$ ]]
  cosign sign --yes --signing-config "${signing_config}" --key "${signing_key}" "${reference}"
  cosign verify --insecure-ignore-tlog=true --key "${signing_pub}" "${reference}" >/dev/null
  printf '%s\t%s\n' "${service}" "${reference}" >> "${images}"
done < <(python3 scripts/ci/edge_app_release_builder.py inventory)

python3 scripts/ci/edge_app_release_builder.py manifest "${metadata}" "${images}" "${manifest}"
cosign sign-blob --yes --signing-config "${signing_config}" --key "${signing_key}" \
  --bundle "${bundle}" "${manifest}" >/dev/null
python3 scripts/ci/edge_app_release_builder.py complete "${manifest}" "${bundle}"
