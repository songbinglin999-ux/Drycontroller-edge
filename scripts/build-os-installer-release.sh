#!/usr/bin/env bash
#
# Build a release-grade DryController OS Installer ISO.
#
# This is the only supported release entrypoint for OS Installer media. It
# rebuilds every moving part, remasters the ISO, runs the final ISO checklist,
# and writes a manifest that the USB writer must verify before touching a disk.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

CHANNEL="release"
BASE_ISO="dist/drycontroller-os-installer-hybrid.iso"
OUTPUT_ISO=""
WIFI_ENV="dist/autoinstall/wifi.env"
PASSWORD_ENV_FILE=""
PASSWORD_HASH_FILE=""
PASSWORD_HASH=""
APT_MIRROR="${DRYCONTROLLER_AUTOINSTALL_APT_MIRROR:-}"
INCLUDE_WIFI=false
FORCE=false

usage() {
  cat <<'EOF'
Usage:
  bash scripts/build-os-installer-release.sh [--channel NAME] [--output dist/OSYYYYMMDD-NAME.iso]
                                                [--password-env-file FILE | --password-hash-file FILE | --password-hash HASH]
                                                [--apt-mirror URI] [--include-wifi] [--force]

Builds a full clean-room OS Installer release:
  1. Require a clean git worktree.
  2. Rebuild service Docker images and dist/bundle-images.tar.
  3. Rebuild the offline apt repo in an Ubuntu 24.04 Docker container.
  4. Rebuild dist/autoinstall with a real dryops sha512-crypt hash.
  5. Remaster the ISO and run the final ISO release checklist.
  6. Generate a matching .manifest.json next to the ISO.

Default password source:
  infra/autoinstall/password.env, when present.

The resulting ISO is not writeable by scripts/write-os-installer-usb.sh unless
the manifest exists, matches the ISO checksum, and records a passing checklist.

dist/autoinstall/wifi.env is refused by default. Pass --include-wifi only when
the release intentionally embeds site WiFi credentials.
EOF
}

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

die() {
  log "ERROR: $*" >&2
  exit 1
}

run() {
  log "$*"
  "$@"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required"
}

require_file() {
  [ -f "$1" ] || die "required file not found: $1"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --channel)
      CHANNEL="${2:-}"
      [ -n "$CHANNEL" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    --output)
      OUTPUT_ISO="${2:-}"
      [ -n "$OUTPUT_ISO" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    --base-iso)
      BASE_ISO="${2:-}"
      [ -n "$BASE_ISO" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    --password-env-file)
      PASSWORD_ENV_FILE="${2:-}"
      [ -n "$PASSWORD_ENV_FILE" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    --password-hash-file)
      PASSWORD_HASH_FILE="${2:-}"
      [ -n "$PASSWORD_HASH_FILE" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    --password-hash)
      PASSWORD_HASH="${2:-}"
      [ -n "$PASSWORD_HASH" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    --apt-mirror)
      APT_MIRROR="${2:-}"
      [ -n "$APT_MIRROR" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    --include-wifi)
      INCLUDE_WIFI=true
      shift
      ;;
    --force)
      FORCE=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

case "$CHANNEL" in
  *[!A-Za-z0-9._-]*|"")
    die "channel must contain only letters, numbers, dot, underscore, or dash"
    ;;
esac

cd "$REPO_ROOT"

require_cmd git
require_cmd docker
require_cmd python3
require_cmd bsdtar
require_cmd shasum

if [ -n "$(git status --porcelain=v1)" ]; then
  git status --short >&2
  die "git worktree must be clean before building a release ISO"
fi

if [ -z "$OUTPUT_ISO" ]; then
  OUTPUT_ISO="dist/OS$(TZ=Asia/Shanghai date '+%Y%m%d')-${CHANNEL}.iso"
fi
MANIFEST="${OUTPUT_ISO%.iso}.manifest.json"

run python3 - "$BASE_ISO" "$OUTPUT_ISO" "$MANIFEST" <<'PY'
from __future__ import annotations

from pathlib import Path
import sys

base_iso, output_iso, manifest = (Path(arg).resolve() for arg in sys.argv[1:])
if output_iso == base_iso:
    raise SystemExit("output ISO must not be the base ISO")
if manifest == base_iso:
    raise SystemExit("manifest path must not be the base ISO")
if output_iso == manifest:
    raise SystemExit("output ISO and manifest paths must differ")
PY

require_file "$BASE_ISO"

if [ -n "$PASSWORD_ENV_FILE" ]; then
  [ -z "$PASSWORD_HASH_FILE" ] && [ -z "$PASSWORD_HASH" ] || die "use only one password source"
elif [ -n "$PASSWORD_HASH_FILE" ] || [ -n "$PASSWORD_HASH" ]; then
  [ -z "$PASSWORD_HASH_FILE" ] || [ -z "$PASSWORD_HASH" ] || die "use only one password source"
elif [ -f "infra/autoinstall/password.env" ]; then
  PASSWORD_ENV_FILE="infra/autoinstall/password.env"
else
  die "no password source; pass --password-env-file, --password-hash-file, or --password-hash"
fi

if [ -e "$OUTPUT_ISO" ] || [ -e "$MANIFEST" ]; then
  [ "$FORCE" = true ] || die "output exists; pass --force or choose another channel/output"
fi
rm -f "$OUTPUT_ISO" "$MANIFEST"

if [ "$INCLUDE_WIFI" = true ]; then
  require_file "$WIFI_ENV"
elif [ -f "$WIFI_ENV" ]; then
  die "dist/autoinstall/wifi.env exists; pass --include-wifi to embed it intentionally, or remove it"
fi

export DRYCONTROLLER_RELEASE_WRAPPER=1

run bash tools/build-bundle-images.sh

log "rebuilding offline apt repository in ubuntu:24.04..."
run docker run --rm \
  -e APT_MIRROR="$APT_MIRROR" \
  -v "$REPO_ROOT:/work" \
  -w /work \
  ubuntu:24.04 \
  bash -lc '
    set -euo pipefail
    apt-get update >/dev/null
    DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates dpkg-dev gzip >/dev/null
    args=(--out dist/autoinstall/drycontroller-apt --suite noble --arch amd64)
    if [ -n "${APT_MIRROR:-}" ]; then
      args+=(--mirror "$APT_MIRROR")
    fi
    bash scripts/build-autoinstall-offline-apt-repo.sh "${args[@]}"
  '

bundle_args=(--out dist/autoinstall)
if [ -n "$PASSWORD_ENV_FILE" ]; then
  require_file "$PASSWORD_ENV_FILE"
  bundle_args+=(--password-env-file "$PASSWORD_ENV_FILE")
elif [ -n "$PASSWORD_HASH_FILE" ]; then
  require_file "$PASSWORD_HASH_FILE"
  bundle_args+=(--password-hash-file "$PASSWORD_HASH_FILE")
else
  bundle_args+=(--password-hash "$PASSWORD_HASH")
fi
run bash scripts/build-autoinstall-bundle.sh "${bundle_args[@]}"

require_file "dist/bundle-images.tar"
require_file "dist/drycontroller-edge.tar.gz"
require_file "dist/autoinstall/drycontroller-apt/Packages"
require_file "dist/autoinstall/drycontroller-apt/Packages.gz"
require_file "dist/autoinstall/activation-bundle.json"

run python3 - "$BASE_ISO" "$OUTPUT_ISO" <<'PY'
from __future__ import annotations

import importlib.util
import sys

base_iso, output_iso = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location(
    "maker",
    "tools/os-installer-usb-maker/os_installer_usb_maker.py",
)
maker = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(maker)


class App:
    def _append_log(self, message: str, level: str = "info") -> None:
        print(f"[{level}] {message}")


maker.build_iso(
    App(),
    {
        "base_iso": base_iso,
        "output_iso": output_iso,
        "include_images": True,
    },
)
PY

run python3 - "$OUTPUT_ISO" "$MANIFEST" "$CHANNEL" "$BASE_ISO" "$INCLUDE_WIFI" <<'PY'
from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import sys
import tarfile
from pathlib import Path

iso = Path(sys.argv[1])
manifest_path = Path(sys.argv[2])
channel = sys.argv[3]
base_iso = Path(sys.argv[4])
include_wifi = sys.argv[5] == "true"


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def json_file_fingerprint(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def image_entries(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    rows: list[dict[str, object]] = []
    with tarfile.open(path) as tf:
        manifest_member = tf.extractfile("manifest.json")
        if manifest_member is None:
            return []
        manifest = json.load(manifest_member)
        for item in manifest:
            config_name = item.get("Config")
            config_bytes = b""
            created = None
            if config_name:
                config_member = tf.extractfile(config_name)
                if config_member is not None:
                    config_bytes = config_member.read()
                    try:
                        created = json.loads(config_bytes.decode("utf-8")).get("created")
                    except Exception:
                        created = None
            rows.append(
                {
                    "repo_tags": item.get("RepoTags") or [],
                    "image_id": "sha256:" + hashlib.sha256(config_bytes).hexdigest() if config_bytes else None,
                    "created": created,
                }
            )
    return rows


source_bundle = Path("dist/drycontroller-edge.tar.gz")
image_bundle = Path("dist/bundle-images.tar")
activation_bundle = Path("dist/autoinstall/activation-bundle.json")
wifi_env = Path("dist/autoinstall/wifi.env")
apt_pool = Path("dist/autoinstall/drycontroller-apt/pool")
status = git("status", "--porcelain=v1")

manifest = {
    "schema": "drycontroller.os-installer-release.v1",
    "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    "release": {
        "channel": channel,
        "clean_room": True,
        "include_images": True,
        "include_wifi": include_wifi,
    },
    "git": {
        "branch": git("branch", "--show-current"),
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(status),
        "status": status.splitlines(),
    },
    "artifacts": {
        "iso": {"path": rel(iso), "sha256": sha256(iso), "size": iso.stat().st_size},
        "base_iso": {"path": rel(base_iso), "sha256": sha256(base_iso), "size": base_iso.stat().st_size},
        "source_bundle": {
            "path": rel(source_bundle),
            "sha256": sha256(source_bundle),
            "size": source_bundle.stat().st_size,
        },
        "image_bundle": {
            "path": rel(image_bundle),
            "sha256": sha256(image_bundle),
            "size": image_bundle.stat().st_size,
            "images": image_entries(image_bundle),
        },
        "offline_apt": {
            "path": "dist/autoinstall/drycontroller-apt",
            "deb_count": len(list(apt_pool.glob("*.deb"))),
            "packages_sha256": sha256(Path("dist/autoinstall/drycontroller-apt/Packages")),
            "packages_gz_sha256": sha256(Path("dist/autoinstall/drycontroller-apt/Packages.gz")),
            "manifest": Path("dist/autoinstall/drycontroller-apt/drycontroller-apt-manifest.txt").read_text(
                encoding="utf-8"
            ).splitlines(),
        },
        "activation_bundle": {
            "path": rel(activation_bundle),
            "sha256": sha256(activation_bundle),
            "fingerprint": json_file_fingerprint(activation_bundle),
        },
        "wifi_env": (
            {"included": True, "path": rel(wifi_env), "sha256": sha256(wifi_env), "size": wifi_env.stat().st_size}
            if include_wifi
            else {"included": False}
        ),
    },
    "checks": {
        "final_iso_release_checklist": {
            "passed": True,
            "checker": "tools/os-installer-usb-maker/os_installer_usb_maker.py::verify_final_iso",
        }
    },
}

if manifest["git"]["dirty"]:
    raise SystemExit("release manifest refuses dirty git state")
if manifest["artifacts"]["offline_apt"]["deb_count"] <= 0:
    raise SystemExit("offline apt repo contains no deb packages")
if not manifest["artifacts"]["image_bundle"]["images"]:
    raise SystemExit("image bundle contains no image manifest entries")
if manifest["artifacts"]["wifi_env"]["included"] != include_wifi:
    raise SystemExit("wifi manifest state mismatch")

manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"release manifest written: {manifest_path}")
print(f"iso sha256: {manifest['artifacts']['iso']['sha256']}")
PY

log "release ISO ready: $OUTPUT_ISO"
log "release manifest ready: $MANIFEST"
