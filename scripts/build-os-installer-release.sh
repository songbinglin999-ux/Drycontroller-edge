#!/usr/bin/env bash
# The only supported release entrypoint for RB-6 OS Installer ISO artifacts.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CHANNEL="release"
BASE_ISO="dist/drycontroller-os-installer-hybrid.iso"
OUTPUT_ISO=""
HOST_RELEASE="dist/initial-release/host-release.json"
SOFTWARE_RELEASE="dist/initial-release/software-release.json"
HOST_PUBLIC_KEY="dist/initial-release/host-release-cosign.pub"
SOFTWARE_PUBLIC_KEY="dist/initial-release/software-release-cosign.pub"
COSIGN_LINUX_AMD64="dist/initial-release/cosign-linux-amd64"
DEVICE_IDENTITY_CLOUD_PUBLIC_KEY="dist/initial-release/device-identity-cloud.pub"
PUBLIC_CONFIG="dist/initial-release/public-config.json"
INSTALLER_PRIVATE_KEY=""
INSTALLER_PUBLIC_KEY=""
APT_MIRROR="${DRYCONTROLLER_AUTOINSTALL_APT_MIRROR:-}"
FORCE=false
COSIGN_APPROVED_VERSION="v3.1.3"
COSIGN_APPROVED_LINUX_AMD64_SHA256="4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71"
PINNED_TOOL_DIR=""

cleanup() {
  if [ -n "$PINNED_TOOL_DIR" ] && [ -d "$PINNED_TOOL_DIR" ]; then
    rm -rf "$PINNED_TOOL_DIR"
  fi
}
trap cleanup EXIT

usage() {
  cat <<'EOF'
Usage:
  bash scripts/build-os-installer-release.sh \
    --installer-private-key FILE --installer-public-key FILE [options]

Options:
  --base-iso FILE
  --output FILE
  --channel NAME
  --host-release FILE
  --software-release FILE
  --host-public-key FILE
  --software-public-key FILE
  --cosign-linux-amd64 FILE
  --device-identity-cloud-key FILE
  --public-config FILE
  --installer-private-key FILE
  --installer-public-key FILE
  --apt-mirror URI
  --force

This wrapper runs only on the fixed Ubuntu 24.04 Linux amd64 builder. It
rebuilds source/images, signed H1/T1/S1 media archives, the offline apt repo,
the identity wheelhouse, the ISO, the final checklist, and a signed adjacent
manifest in one invocation. It never writes a USB disk.
EOF
}

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }
run() { log "$*"; "$@"; }
require_file() { [ -f "$1" ] || die "required file not found: $1"; }
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "$1 is required"; }
value() { [ -n "${2:-}" ] || die "$1 requires a value"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --base-iso) value "$1" "${2:-}"; BASE_ISO="$2"; shift 2 ;;
    --output) value "$1" "${2:-}"; OUTPUT_ISO="$2"; shift 2 ;;
    --channel) value "$1" "${2:-}"; CHANNEL="$2"; shift 2 ;;
    --host-release) value "$1" "${2:-}"; HOST_RELEASE="$2"; shift 2 ;;
    --software-release) value "$1" "${2:-}"; SOFTWARE_RELEASE="$2"; shift 2 ;;
    --host-public-key) value "$1" "${2:-}"; HOST_PUBLIC_KEY="$2"; shift 2 ;;
    --software-public-key) value "$1" "${2:-}"; SOFTWARE_PUBLIC_KEY="$2"; shift 2 ;;
    --cosign-linux-amd64) value "$1" "${2:-}"; COSIGN_LINUX_AMD64="$2"; shift 2 ;;
    --device-identity-cloud-key) value "$1" "${2:-}"; DEVICE_IDENTITY_CLOUD_PUBLIC_KEY="$2"; shift 2 ;;
    --public-config) value "$1" "${2:-}"; PUBLIC_CONFIG="$2"; shift 2 ;;
    --installer-private-key) value "$1" "${2:-}"; INSTALLER_PRIVATE_KEY="$2"; shift 2 ;;
    --installer-public-key) value "$1" "${2:-}"; INSTALLER_PUBLIC_KEY="$2"; shift 2 ;;
    --apt-mirror) value "$1" "${2:-}"; APT_MIRROR="$2"; shift 2 ;;
    --force) FORCE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

case "$CHANNEL" in *[!A-Za-z0-9._-]*|"") die "channel contains invalid characters" ;; esac
cd "$REPO_ROOT"
for command in git docker python3 pip3 xorriso bsdtar sha256sum; do require_cmd "$command"; done
[ "$(uname -s)" = "Linux" ] && [ "$(uname -m)" = "x86_64" ] \
  || die "formal media requires the fixed Linux amd64 builder"
# shellcheck disable=SC1091
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] && [ "${VERSION_ID:-}" = "24.04" ] \
  || die "formal media requires Ubuntu 24.04"
python3 - <<'PY'
import sys
if sys.version_info[:2] != (3, 12):
    raise SystemExit("formal media requires Python 3.12")
PY
[ "$(docker info --format '{{.Architecture}}')" = "x86_64" ] \
  || [ "$(docker info --format '{{.Architecture}}')" = "amd64" ] \
  || die "Docker builder must be amd64"
[ -z "$(git status --porcelain=v1)" ] || { git status --short >&2; die "release worktree must be clean"; }

for path in \
  "$BASE_ISO" "$HOST_RELEASE" "$SOFTWARE_RELEASE" "$HOST_PUBLIC_KEY" \
  "$SOFTWARE_PUBLIC_KEY" "$COSIGN_LINUX_AMD64" "$DEVICE_IDENTITY_CLOUD_PUBLIC_KEY" \
  "$PUBLIC_CONFIG" "$INSTALLER_PRIVATE_KEY" "$INSTALLER_PUBLIC_KEY"; do
  require_file "$path"
done
PINNED_TOOL_DIR="$(mktemp -d)"
install -m 0755 "$COSIGN_LINUX_AMD64" "$PINNED_TOOL_DIR/cosign"
[ "$(sha256sum "$PINNED_TOOL_DIR/cosign" | awk '{print $1}')" = "$COSIGN_APPROVED_LINUX_AMD64_SHA256" ] \
  || die "formal builder cosign checksum is not approved"
cosign_version_output="$("$PINNED_TOOL_DIR/cosign" version 2>/dev/null)" \
  || die "formal builder cosign cannot report its version"
grep -Eq "GitVersion:[[:space:]]+${COSIGN_APPROVED_VERSION//./\\.}([[:space:]]|$)" <<<"$cosign_version_output" \
  || die "formal builder cosign version is not approved"
grep -Eq 'Platform:[[:space:]]+linux/amd64([[:space:]]|$)' <<<"$cosign_version_output" \
  || die "formal builder cosign platform is not linux/amd64"
export PATH="$PINNED_TOOL_DIR:$PATH"
if [ -z "$OUTPUT_ISO" ]; then
  OUTPUT_ISO="dist/OS$(TZ=Asia/Shanghai date '+%Y%m%d')-${CHANNEL}.iso"
fi
MANIFEST="${OUTPUT_ISO%.iso}.manifest.json"
SIGNATURE="${OUTPUT_ISO%.iso}.manifest.sigstore.json"
python3 - "$BASE_ISO" "$OUTPUT_ISO" "$MANIFEST" "$SIGNATURE" "$REPO_ROOT/dist" <<'PY'
import sys
from pathlib import Path
paths = [Path(value).resolve() for value in sys.argv[1:5]]
dist = Path(sys.argv[5]).resolve()
if len(set(paths)) != len(paths):
    raise SystemExit("base ISO, output ISO, manifest, and signature paths must differ")
if paths[1].suffix != ".iso" or any(path.suffix != ".json" for path in paths[2:]):
    raise SystemExit("release output extensions are invalid")
for path in paths[1:]:
    if path.parent != dist:
        raise SystemExit("release outputs must be direct files under repository dist/")
PY
if [ -e "$OUTPUT_ISO" ] || [ -e "$MANIFEST" ] || [ -e "$SIGNATURE" ]; then
  [ "$FORCE" = true ] || die "release output exists; choose another output or pass --force"
  rm -f "$OUTPUT_ISO" "$MANIFEST" "$SIGNATURE"
fi

export DRYCONTROLLER_RELEASE_WRAPPER=1
run bash tools/build-bundle-images.sh
run python3 scripts/prepare-initial-release.py \
  --host-release "$HOST_RELEASE" \
  --host-cosign-public-key "$HOST_PUBLIC_KEY" \
  --software-release "$SOFTWARE_RELEASE" \
  --software-cosign-public-key "$SOFTWARE_PUBLIC_KEY" \
  --cosign-linux-amd64 "$COSIGN_LINUX_AMD64" \
  --source-commit "$(git rev-parse HEAD)" \
  --output-dir dist/autoinstall \
  --host-image-bundle dist/host-image.tar \
  --software-image-bundle dist/software-images.tar
run bash scripts/build-device-identity-wheelhouse.sh
apt_args=(--suite noble --arch amd64)
if [ -n "$APT_MIRROR" ]; then apt_args+=(--mirror "$APT_MIRROR"); fi
run bash scripts/build-autoinstall-offline-apt-repo.sh "${apt_args[@]}"
run bash scripts/build-autoinstall-bundle.sh --out dist/autoinstall
install -m 0644 "$DEVICE_IDENTITY_CLOUD_PUBLIC_KEY" dist/autoinstall/device-identity-cloud.pub
install -m 0644 "$PUBLIC_CONFIG" dist/autoinstall/public-config.json

run python3 - "$BASE_ISO" "$OUTPUT_ISO" <<'PY'
import importlib.util
import sys
spec = importlib.util.spec_from_file_location("maker", "tools/os-installer-usb-maker/os_installer_usb_maker.py")
maker = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(maker)
class App:
    def _append_log(self, message, level="info"):
        print(f"[{level}] {message}")
maker.build_iso(App(), {"base_iso": sys.argv[1], "output_iso": sys.argv[2], "include_images": True})
PY
run python3 scripts/create-os-installer-manifest.py \
  --iso "$OUTPUT_ISO" \
  --base-iso "$BASE_ISO" \
  --output "$MANIFEST" \
  --channel "$CHANNEL" \
  --installer-public-key "$INSTALLER_PUBLIC_KEY"
SIGNING_CONFIG="$PINNED_TOOL_DIR/signing-config.json"
run cosign signing-config create \
  --no-default-fulcio \
  --no-default-oidc \
  --no-default-rekor \
  --no-default-tsa \
  --out "$SIGNING_CONFIG"
run cosign sign-blob \
  --yes \
  --signing-config "$SIGNING_CONFIG" \
  --key "$INSTALLER_PRIVATE_KEY" \
  --bundle "$SIGNATURE" \
  "$MANIFEST"
run cosign verify-blob \
  --key "$INSTALLER_PUBLIC_KEY" \
  --insecure-ignore-tlog=true \
  --bundle "$SIGNATURE" \
  "$MANIFEST"
log "release ISO ready: $OUTPUT_ISO"
log "signed release manifest ready: $MANIFEST + $SIGNATURE"
