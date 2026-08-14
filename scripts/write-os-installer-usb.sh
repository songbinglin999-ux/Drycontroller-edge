#!/usr/bin/env bash
#
# Write a release-grade DryController OS Installer ISO to a USB disk.
#
# This script refuses to write unless the ISO has a matching release manifest
# and still passes the final ISO checklist immediately before the destructive
# disk operation.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

ISO=""
MANIFEST=""
SIGNATURE=""
INSTALLER_PUBLIC_KEY=""
DISK_ID=""
YES=false

usage() {
  cat <<'EOF'
Usage:
  bash scripts/write-os-installer-usb.sh dist/OSYYYYMMDD-channel.iso \
    --installer-public-key FILE --disk diskN [--yes]

Required:
  - ISO path
  - matching .manifest.json next to the ISO, or --manifest FILE
  - matching .manifest.sigstore.json, or --signature FILE
  - independently supplied OS Installer verification key
  - explicit external disk id, for example --disk disk3

This command is destructive. It validates the release manifest, re-runs the ISO
checklist, confirms the target is an external physical whole disk, then writes
via /dev/rdiskN.

To discover the current external USB disk:
  diskutil list external physical
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

while [ $# -gt 0 ]; do
  case "$1" in
    --manifest)
      MANIFEST="${2:-}"
      [ -n "$MANIFEST" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    --signature)
      SIGNATURE="${2:-}"
      [ -n "$SIGNATURE" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    --installer-public-key)
      INSTALLER_PUBLIC_KEY="${2:-}"
      [ -n "$INSTALLER_PUBLIC_KEY" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    --disk)
      DISK_ID="${2:-}"
      [ -n "$DISK_ID" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    --yes)
      YES=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --*)
      usage >&2
      echo "unknown argument: $1" >&2
      exit 2
      ;;
    *)
      if [ -n "$ISO" ]; then
        usage >&2
        echo "unexpected extra argument: $1" >&2
        exit 2
      fi
      ISO="$1"
      shift
      ;;
  esac
done

[ -n "$ISO" ] || { usage >&2; exit 2; }
[ -f "$ISO" ] || die "ISO not found: $ISO"
ISO="$(cd "$(dirname "$ISO")" && pwd)/$(basename "$ISO")"
if [ -z "$MANIFEST" ]; then
  MANIFEST="${ISO%.iso}.manifest.json"
fi
if [ -z "$SIGNATURE" ]; then
  SIGNATURE="${ISO%.iso}.manifest.sigstore.json"
fi
[ -f "$MANIFEST" ] || die "manifest not found: $MANIFEST"
[ -f "$SIGNATURE" ] || die "manifest signature not found: $SIGNATURE"
[ -n "$INSTALLER_PUBLIC_KEY" ] || die "--installer-public-key is required"
[ -f "$INSTALLER_PUBLIC_KEY" ] || die "installer public key not found: $INSTALLER_PUBLIC_KEY"
MANIFEST="$(cd "$(dirname "$MANIFEST")" && pwd)/$(basename "$MANIFEST")"
SIGNATURE="$(cd "$(dirname "$SIGNATURE")" && pwd)/$(basename "$SIGNATURE")"
INSTALLER_PUBLIC_KEY="$(cd "$(dirname "$INSTALLER_PUBLIC_KEY")" && pwd)/$(basename "$INSTALLER_PUBLIC_KEY")"

require_cmd python3
require_cmd bsdtar
require_cmd diskutil
require_cmd cosign

export DRYCONTROLLER_RELEASE_WRAPPER=1

cd "$REPO_ROOT"

run cosign verify-blob \
  --key "$INSTALLER_PUBLIC_KEY" \
  --insecure-ignore-tlog=true \
  --bundle "$SIGNATURE" \
  "$MANIFEST"

run python3 - "$ISO" "$MANIFEST" "$INSTALLER_PUBLIC_KEY" <<'PY'
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

iso = Path(sys.argv[1])
manifest_path = Path(sys.argv[2])
installer_public_key = Path(sys.argv[3])


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if manifest.get("schema") != "drycontroller.os-installer-release.v2":
    raise SystemExit("manifest schema mismatch")
if not manifest.get("checks", {}).get("final_iso_release_checklist", {}).get("passed"):
    raise SystemExit("manifest does not record a passing final ISO checklist")
expected_sha = manifest.get("artifacts", {}).get("iso", {}).get("sha256")
actual_sha = sha256(iso)
if expected_sha != actual_sha:
    raise SystemExit(f"ISO sha256 mismatch: manifest={expected_sha} actual={actual_sha}")
if not manifest.get("release", {}).get("include_images") or not manifest.get("release", {}).get("public_material_only"):
    raise SystemExit("release manifest must include bundled Docker images")
key_digest = "sha256:" + sha256(installer_public_key)
if manifest.get("installer_signing_key_sha256") != key_digest:
    raise SystemExit("release manifest signing key mismatch")

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


maker.verify_final_iso(App(), iso, include_images=True)
print(f"manifest ok: {manifest_path}")
print(f"iso sha256: {actual_sha}")
PY

if [ -z "$DISK_ID" ]; then
  diskutil list external physical || true
  die "pass --disk diskN after verifying the external USB above"
fi

DISK_ID="${DISK_ID#/dev/}"
case "$DISK_ID" in
  disk[0-9]*)
    ;;
  *)
    die "--disk must look like diskN, got: $DISK_ID"
    ;;
esac

DISK="/dev/${DISK_ID}"
RAW_DISK="/dev/r${DISK_ID}"

run diskutil list external physical
run diskutil list "$DISK"

verify_target() {
python3 - "$DISK" <<'PY'
from __future__ import annotations

import plistlib
import subprocess
import sys

disk = sys.argv[1]
disk_id = disk.removeprefix("/dev/")

external_raw = subprocess.check_output(["diskutil", "list", "-plist", "external", "physical"])
external_payload = plistlib.loads(external_raw)
external_ids = {
    item.get("DeviceIdentifier")
    for item in external_payload.get("AllDisksAndPartitions", [])
    if item.get("DeviceIdentifier")
}
if disk_id not in external_ids:
    raise SystemExit(f"refusing to write non-external-physical disk: {disk}")

raw = subprocess.check_output(["diskutil", "info", "-plist", disk])
payload = plistlib.loads(raw)
if payload.get("VirtualOrPhysical") != "Physical":
    raise SystemExit(f"refusing to write non-physical disk: {disk}")
if payload.get("Internal") is not False:
    raise SystemExit(f"refusing to write internal disk: {disk}")
if payload.get("WholeDisk") is not True:
    raise SystemExit(f"target must be a whole disk, not a partition: {disk}")
PY
}

run verify_target

if [ "$YES" != true ]; then
  printf 'This will erase %s. Type "ERASE %s" to continue: ' "$DISK" "$DISK_ID" >&2
  read -r confirmation
  [ "$confirmation" = "ERASE ${DISK_ID}" ] || die "confirmation did not match; aborting"
fi

run verify_target
run diskutil unmountDisk force "$DISK"
run sudo /bin/dd "if=$ISO" "of=$RAW_DISK" bs=4m status=progress
run sync
run sudo python3 - "$ISO" "$RAW_DISK" <<'PY'
import hashlib
import sys
from pathlib import Path

iso = Path(sys.argv[1])
raw_disk = Path(sys.argv[2])
remaining = iso.stat().st_size
expected_hash = hashlib.sha256()
with iso.open("rb") as handle:
    for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
        expected_hash.update(chunk)
expected = expected_hash.hexdigest()
actual = hashlib.sha256()
with raw_disk.open("rb", buffering=0) as handle:
    while remaining:
        chunk = handle.read(min(4 * 1024 * 1024, remaining))
        if not chunk:
            raise SystemExit("USB raw compare ended before the ISO length")
        actual.update(chunk)
        remaining -= len(chunk)
if actual.hexdigest() != expected:
    raise SystemExit("USB raw compare sha256 mismatch")
print(f"USB raw compare passed: {expected}")
PY
run diskutil eject "$DISK"
log "write complete: $ISO -> $DISK"
