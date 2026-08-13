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
DISK_ID=""
YES=false

usage() {
  cat <<'EOF'
Usage:
  bash scripts/write-os-installer-usb.sh dist/OSYYYYMMDD-channel.iso --disk diskN [--yes]

Required:
  - ISO path
  - matching .manifest.json next to the ISO, or --manifest FILE
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
[ -f "$MANIFEST" ] || die "manifest not found: $MANIFEST"
MANIFEST="$(cd "$(dirname "$MANIFEST")" && pwd)/$(basename "$MANIFEST")"

require_cmd python3
require_cmd shasum
require_cmd bsdtar
require_cmd diskutil

export DRYCONTROLLER_RELEASE_WRAPPER=1

cd "$REPO_ROOT"

run python3 - "$ISO" "$MANIFEST" <<'PY'
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

iso = Path(sys.argv[1])
manifest_path = Path(sys.argv[2])


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if manifest.get("schema") != "drycontroller.os-installer-release.v1":
    raise SystemExit("manifest schema mismatch")
if not manifest.get("checks", {}).get("final_iso_release_checklist", {}).get("passed"):
    raise SystemExit("manifest does not record a passing final ISO checklist")
expected_sha = manifest.get("artifacts", {}).get("iso", {}).get("sha256")
actual_sha = sha256(iso)
if expected_sha != actual_sha:
    raise SystemExit(f"ISO sha256 mismatch: manifest={expected_sha} actual={actual_sha}")
if not manifest.get("release", {}).get("include_images"):
    raise SystemExit("release manifest must include bundled Docker images")

approved_cosign = {
    "version": "v3.1.3",
    "linux_amd64_sha256": "sha256:4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71",
}
initial_release = manifest.get("artifacts", {}).get("initial_release")
if not isinstance(initial_release, dict):
    raise SystemExit("release manifest initial SoftwareRelease identity missing")
if initial_release.get("cosign_version") != approved_cosign["version"]:
    raise SystemExit("release manifest cosign version is not approved")
if "sha256:" + str(initial_release.get("cosign_linux_amd64_sha256")) != approved_cosign["linux_amd64_sha256"]:
    raise SystemExit("release manifest cosign checksum is not approved")

spec = importlib.util.spec_from_file_location(
    "maker",
    "tools/os-installer-usb-maker/os_installer_usb_maker.py",
)
maker = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(maker)

seed_bytes = maker.extract_iso_file(iso, "software-release-seed.json")
host_active_bytes = maker.extract_iso_file(iso, "host-active.json")
public_key_bytes = maker.extract_iso_file(iso, "cosign.pub")
cosign_bytes = maker.extract_iso_file(iso, "cosign-linux-amd64")
if hashlib.sha256(seed_bytes).hexdigest() != initial_release.get("seed_sha256"):
    raise SystemExit("embedded SoftwareRelease seed checksum mismatch")
if hashlib.sha256(host_active_bytes).hexdigest() != initial_release.get("host_active_sha256"):
    raise SystemExit("embedded active HostRelease state checksum mismatch")
if hashlib.sha256(public_key_bytes).hexdigest() != initial_release.get("cosign_public_key_sha256"):
    raise SystemExit("embedded cosign public key checksum mismatch")
if hashlib.sha256(cosign_bytes).hexdigest() != initial_release.get("cosign_linux_amd64_sha256"):
    raise SystemExit("embedded cosign binary checksum mismatch")
seed = json.loads(seed_bytes)
seed_manifest = seed.get("manifest") if isinstance(seed, dict) else None
if not isinstance(seed_manifest, dict) or seed_manifest.get("cosign") != approved_cosign:
    raise SystemExit("embedded SoftwareRelease cosign toolchain is not approved")
canonical_seed_manifest = json.dumps(
    seed_manifest,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
seed_manifest_digest = "sha256:" + hashlib.sha256(canonical_seed_manifest).hexdigest()
if seed.get("manifest_digest") != seed_manifest_digest or initial_release.get("manifest_digest") != seed_manifest_digest:
    raise SystemExit("embedded SoftwareRelease manifest digest mismatch")
host_active = json.loads(host_active_bytes)
if (
    seed_manifest.get("host_release_id") != host_active.get("host_release_id")
    or seed_manifest.get("topology_digest") != host_active.get("topology_digest")
    or initial_release.get("host_release_id") != host_active.get("host_release_id")
    or initial_release.get("topology_digest") != host_active.get("topology_digest")
    or initial_release.get("host_release_manifest_digest")
    != host_active.get("host_release_manifest_digest")
):
    raise SystemExit("embedded SoftwareRelease and active HostRelease binding mismatch")


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

run python3 - "$DISK" <<'PY'
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
if payload.get("Internal"):
    raise SystemExit(f"refusing to write internal disk: {disk}")
if payload.get("WholeDisk") is False:
    raise SystemExit(f"target must be a whole disk, not a partition: {disk}")
PY

if [ "$YES" != true ]; then
  printf 'This will erase %s. Type "ERASE %s" to continue: ' "$DISK" "$DISK_ID" >&2
  read -r confirmation
  [ "$confirmation" = "ERASE ${DISK_ID}" ] || die "confirmation did not match; aborting"
fi

run diskutil unmountDisk force "$DISK"
run sudo /bin/dd "if=$ISO" "of=$RAW_DISK" bs=4m status=progress
run sync
run diskutil eject "$DISK"
log "write complete: $ISO -> $DISK"
