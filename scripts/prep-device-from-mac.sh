#!/usr/bin/env bash
# One-command office prep: vanilla Ubuntu Server 24.04 IPC -> lane-ready
# unregistered factory Edge device (audit item FACTORY-DEVICE-PREP-20260903).
#
# Assembles a secret-free bundle (exact repo tree, verified identity
# wheelhouse built on the Linux staging ECS, pinned tailscale deb, cloud
# identity PUBLIC key derived read-only on the ECS, public line config,
# operator SSH public key), pushes it to the device and runs
# infra/bootstrap/prep-factory-device.sh there.
#
# The only per-device secrets are generated on the device itself.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EDGE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"

TARGET=""
INTERFACE=""
CLOUD_URL="https://ops.example.com"
TAILNET_ADDRESS="100.100.0.10"
LOGIN_SERVER="https://headscale.example.com"
AUTHORIZED_KEYS=""
CACHE="${HOME}/.cache/drycontroller/device-prep"
VERSIONS_LOCK="${DRYCONTROLLER_VERSIONS_LOCK:-${EDGE_ROOT}/../cloud/infra/headscale/versions.lock.json}"
ECS_ALIAS="edge-build-host"
CLOUD_OPS_CONTAINER="drycontroller-cloud-ops"
SHUTDOWN_ARG=""

fail() {
  printf '[prep-device-from-mac] %s\n' "$1" >&2
  exit "${2:-1}"
}

usage() {
  cat <<'EOF'
Usage:
  scripts/prep-device-from-mac.sh dryops@192.168.x.x --interface enp2s0 [options]

Options:
  --interface NAME        device NIC cabled to the factory lane (required)
  --cloud-url URL         default https://ops.example.com
  --tailnet-address IP    default 100.100.0.10
  --login-server URL      default https://headscale.example.com
  --authorized-keys FILE  default ~/.ssh/id_ed25519.pub (or id_rsa.pub)
  --cache DIR             bundle cache (default ~/.cache/drycontroller/device-prep)
  --versions-lock FILE    versions.lock.json pinning the tailscale deb (from your cloud project)
  --ecs ALIAS             Linux box used to build the wheelhouse and derive the
                          cloud identity public key (default edge-build-host)
  --shutdown              power the device off after a PASS
EOF
}

required_value() {
  [ -n "${2:-}" ] || fail "$1 requires a value" 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --interface) required_value "$1" "${2:-}"; INTERFACE="$2"; shift 2 ;;
    --cloud-url) required_value "$1" "${2:-}"; CLOUD_URL="$2"; shift 2 ;;
    --tailnet-address) required_value "$1" "${2:-}"; TAILNET_ADDRESS="$2"; shift 2 ;;
    --login-server) required_value "$1" "${2:-}"; LOGIN_SERVER="$2"; shift 2 ;;
    --authorized-keys) required_value "$1" "${2:-}"; AUTHORIZED_KEYS="$2"; shift 2 ;;
    --cache) required_value "$1" "${2:-}"; CACHE="$2"; shift 2 ;;
    --versions-lock) required_value "$1" "${2:-}"; VERSIONS_LOCK="$2"; shift 2 ;;
    --ecs) required_value "$1" "${2:-}"; ECS_ALIAS="$2"; shift 2 ;;
    --shutdown) SHUTDOWN_ARG="--shutdown"; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      if [ -z "$TARGET" ] && [ $# -ge 1 ]; then TARGET="$1"; shift; else usage >&2; fail "unknown argument: $1" 2; fi
      ;;
  esac
done

[ -n "$TARGET" ] || { usage >&2; fail "target (user@host) is required" 2; }
[ -n "$INTERFACE" ] || { usage >&2; fail "--interface is required (the device NIC for the factory lane)" 2; }
[ -f "$VERSIONS_LOCK" ] || fail "versions lock not found: $VERSIONS_LOCK" 2
if [ -z "$AUTHORIZED_KEYS" ]; then
  for candidate in "${HOME}/.ssh/id_ed25519.pub" "${HOME}/.ssh/id_rsa.pub"; do
    if [ -f "$candidate" ]; then AUTHORIZED_KEYS="$candidate"; break; fi
  done
fi
[ -n "$AUTHORIZED_KEYS" ] && [ -f "$AUTHORIZED_KEYS" ] \
  || fail "an SSH public key is required (--authorized-keys)" 2

command -v python3 >/dev/null 2>&1 || fail "python3 is required on this Mac" 2
command -v git >/dev/null 2>&1 || fail "git is required" 2

step() {
  printf '[prep-device-from-mac] %s\n' "$1"
}

EDGE_HEAD="$(git -C "$EDGE_ROOT" rev-parse HEAD)"
if [ -n "$(git -C "$EDGE_ROOT" status --porcelain --untracked-files=no)" ]; then
  fail "edge checkout is dirty; commit first so the bundle is an exact reviewed tree" 2
fi

mkdir -p "$CACHE"

step "read tailscale pin from versions lock"
LOCK_JSON="$(python3 - "$VERSIONS_LOCK" <<'PY'
import json
import sys

lock = json.load(open(sys.argv[1]))
client = lock["tailscale_client"]["linux_amd64_candidate"]
print(json.dumps({
    "schema": "drycontroller.tailscale-client.v1",
    "version": client["version"],
    "architecture": "amd64",
    "deb_url": client["deb_url"],
    "deb_sha256": client["deb_sha256"],
}))
PY
)"
TS_VERSION="$(printf '%s' "$LOCK_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])')"
TS_URL="$(printf '%s' "$LOCK_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["deb_url"])')"
TS_SHA="$(printf '%s' "$LOCK_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["deb_sha256"])')"

step "tailscale ${TS_VERSION}: fetch and verify"
TS_DEB="${CACHE}/tailscale_${TS_VERSION}_amd64.deb"
if [ ! -f "$TS_DEB" ] || [ "$(shasum -a 256 "$TS_DEB" | awk '{print $1}')" != "$TS_SHA" ]; then
  curl -fsSL --retry 3 -o "$TS_DEB" "$TS_URL" \
    || ssh "$ECS_ALIAS" "curl -fsSL --retry 3 -o - '$TS_URL'" > "$TS_DEB" \
    || fail "could not download the pinned tailscale deb" 2
fi
[ "$(shasum -a 256 "$TS_DEB" | awk '{print $1}')" = "$TS_SHA" ] \
  || fail "downloaded tailscale deb checksum mismatch" 2

step "identity wheelhouse (built on ${ECS_ALIAS}, cached locally)"
WHEELHOUSE="${CACHE}/wheelhouse"
WHEELHOUSE_KEY_SOURCE="$(shasum -a 256 "${EDGE_ROOT}/services/device-identity/requirements.txt" | awk '{print $1}') ${EDGE_HEAD}"
WHEELHOUSE_KEY_NOW="${WHEELHOUSE_KEY_SOURCE} $(shasum -a 256 "$TS_DEB" | awk '{print $1}')"
NEED_BUILD=1
if [ -f "${WHEELHOUSE}/SHA256SUMS" ] && [ -f "${CACHE}/wheelhouse.key" ]; then
  [ "$(cat "${CACHE}/wheelhouse.key")" = "$WHEELHOUSE_KEY_NOW" ] && NEED_BUILD=0
fi
if [ "$NEED_BUILD" -eq 1 ]; then
  step "building wheelhouse on ${ECS_ALIAS} (exact tree ${EDGE_HEAD:0:12})"
  git -C "$EDGE_ROOT" archive "$EDGE_HEAD" \
    | ssh "$ECS_ALIAS" 'rm -rf /tmp/dryprep-src && mkdir -p /tmp/dryprep-src && tar x -C /tmp/dryprep-src'
  ssh "$ECS_ALIAS" 'command -v pip3 >/dev/null 2>&1 || sudo -n apt-get install -y -qq python3-pip >/dev/null; python3 /tmp/dryprep-src/scripts/build-device-identity-wheelhouse.sh' \
    || fail "wheelhouse build failed on the ECS (needs python3 3.12 + pip there)" 2
  rm -rf "$WHEELHOUSE"
  mkdir -p "$WHEELHOUSE"
  ssh "$ECS_ALIAS" 'tar czf - -C /tmp/dryprep-src/dist/autoinstall/identity-wheelhouse .' \
    | tar xz -C "$WHEELHOUSE"
  ssh "$ECS_ALIAS" 'rm -rf /tmp/dryprep-src' || true
  printf '%s\n' "$WHEELHOUSE_KEY_NOW" > "${CACHE}/wheelhouse.key"
fi
MANIFEST_SHA="$(shasum -a 256 "${WHEELHOUSE}/SHA256SUMS" | awk '{print $1}')"

step "derive cloud identity public key (read-only on ${ECS_ALIAS})"
PUB_JSON="$(ssh "$ECS_ALIAS" "sudo -n docker exec ${CLOUD_OPS_CONTAINER} python3 -c \"
import base64, os
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
seed = base64.b64decode(os.environ['DEVICE_IDENTITY_SIGNING_PRIVATE_KEY'])
public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()
print(base64.b64encode(public).decode())
\" && sudo -n docker exec ${CLOUD_OPS_CONTAINER} printenv DEVICE_IDENTITY_SIGNING_PUBLIC_KEY_SHA256")" \
  || fail "could not derive the cloud identity public key on the ECS" 2
CLOUD_PUB="$(printf '%s\n' "$PUB_JSON" | sed -n '1p')"
CLOUD_PUB_SHA_ENV="$(printf '%s\n' "$PUB_JSON" | sed -n '2p')"
CLOUD_PUB_SHA="$(printf '%s' "$CLOUD_PUB" | python3 -c 'import base64,hashlib,sys;print(hashlib.sha256(base64.b64decode(sys.stdin.read().strip(), validate=True)).hexdigest())')"
[ "$CLOUD_PUB_SHA" = "$CLOUD_PUB_SHA_ENV" ] \
  || fail "derived public key does not match the cloud-recorded fingerprint" 2

step "assemble bundle"
BUNDLE="${CACHE}/bundle"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/repo" "$BUNDLE/wheelhouse" "$BUNDLE/tailscale"
git -C "$EDGE_ROOT" archive "$EDGE_HEAD" | tar x -C "${BUNDLE}/repo"
cp "${WHEELHOUSE}/SHA256SUMS" "$BUNDLE/wheelhouse/"
for wheel in "${WHEELHOUSE}"/*.whl; do cp "$wheel" "$BUNDLE/wheelhouse/"; done
printf '%s\n' "$MANIFEST_SHA" > "${BUNDLE}/wheelhouse.manifest.sha256"
cp "$TS_DEB" "${BUNDLE}/tailscale/tailscale.deb"
printf '%s\n' "$LOCK_JSON" > "${BUNDLE}/tailscale/tailscale-client.lock.json"
printf '%s\n' "$CLOUD_PUB" > "${BUNDLE}/device-identity-cloud.pub"
printf '%s\n' "$CLOUD_PUB_SHA" > "${BUNDLE}/device-identity-cloud.pub.sha256"
cp "$AUTHORIZED_KEYS" "${BUNDLE}/ssh_authorized_keys"
cp "${EDGE_ROOT}/env.example" "${BUNDLE}/env-template"
python3 - "$BUNDLE/public-config.json" "$CLOUD_URL" "$TAILNET_ADDRESS" "$LOGIN_SERVER" "$INTERFACE" <<'PY'
import json
import sys

payload = {
    "schema": "drycontroller.os-installer-public-config.v3",
    "cloud_ops_base_url": sys.argv[2],
    "cloud_ops_tailnet_address": sys.argv[3],
    "tailscale_login_server": sys.argv[4],
    "factory_interface": sys.argv[5],
    "factory_address": "169.254.203.2/30",
}
with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump(payload, stream, indent=2, sort_keys=True)
    stream.write("\n")
PY

step "push bundle to ${TARGET}"
ssh "$TARGET" 'mkdir -p /tmp/drycontroller-prep'
tar czf - -C "$CACHE" bundle | ssh "$TARGET" 'tar xzf - -C /tmp/drycontroller-prep'

step "run prep on the device (sudo password = the unified rescue password)"
ssh -t "$TARGET" \
  "sudo bash /tmp/drycontroller-prep/bundle/repo/infra/bootstrap/prep-factory-device.sh --bundle /tmp/drycontroller-prep/bundle ${SHUTDOWN_ARG}"

printf '[prep-device-from-mac] device prepped. Put it on the factory lane and power it on;\n'
printf '[prep-device-from-mac] the station watch will activate it zero-touch.\n'
