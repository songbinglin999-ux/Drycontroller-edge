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
PUBLIC_SCAN_TMP_DIR=""
ISO_SOURCE=""

cleanup() {
  exec 9<&- || true
  if [ -n "$PUBLIC_SCAN_TMP_DIR" ] && [ -d "$PUBLIC_SCAN_TMP_DIR" ]; then
    chmod 0700 "$PUBLIC_SCAN_TMP_DIR/snapshot" 2>/dev/null || true
    rm -rf "$PUBLIC_SCAN_TMP_DIR"
  fi
}
trap cleanup EXIT

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
require_cmd unsquashfs
require_cmd zstd
require_cmd diskutil
require_cmd cosign

export DRYCONTROLLER_RELEASE_WRAPPER=1

cd "$REPO_ROOT"

ISO_SOURCE="$ISO"
PUBLIC_SCAN_TMP_DIR="$(mktemp -d)"
SNAPSHOT_DIR="$PUBLIC_SCAN_TMP_DIR/snapshot"
mkdir -m 0700 "$SNAPSHOT_DIR"
run python3 scripts/stable-release-artifact.py snapshot \
  --source "$MANIFEST" \
  --destination "$SNAPSHOT_DIR/manifest.json" \
  --maximum-size 16777216 \
  --timeout-seconds 30
run python3 scripts/stable-release-artifact.py snapshot \
  --source "$SIGNATURE" \
  --destination "$SNAPSHOT_DIR/manifest.sigstore.json" \
  --maximum-size 1048576 \
  --timeout-seconds 30
run python3 scripts/stable-release-artifact.py snapshot \
  --source "$INSTALLER_PUBLIC_KEY" \
  --destination "$SNAPSHOT_DIR/installer.pub" \
  --maximum-size 65536 \
  --timeout-seconds 30
MANIFEST="$SNAPSHOT_DIR/manifest.json"
SIGNATURE="$SNAPSHOT_DIR/manifest.sigstore.json"
INSTALLER_PUBLIC_KEY="$SNAPSHOT_DIR/installer.pub"

run cosign verify-blob \
  --key "$INSTALLER_PUBLIC_KEY" \
  --insecure-ignore-tlog=true \
  --bundle "$SIGNATURE" \
  "$MANIFEST"

EXPECTED_ISO_METADATA="$(python3 - "$MANIFEST" <<'PY'
import json
import re
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
artifact = manifest.get("artifacts", {}).get("iso", {})
digest = artifact.get("sha256")
size = artifact.get("size")
if (
    manifest.get("schema") != "drycontroller.os-installer-release.v3"
    or not isinstance(digest, str)
    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    or not isinstance(size, int)
    or isinstance(size, bool)
    or size < 1
):
    raise SystemExit("signed ISO metadata invalid")
print(f"{size} {digest}")
PY
)" || die "could not read signed ISO metadata"
EXPECTED_ISO_SIZE="${EXPECTED_ISO_METADATA%% *}"
EXPECTED_ISO_SHA256="${EXPECTED_ISO_METADATA#* }"
run python3 scripts/stable-release-artifact.py snapshot \
  --source "$ISO_SOURCE" \
  --destination "$SNAPSHOT_DIR/installer.iso" \
  --maximum-size 17179869184 \
  --timeout-seconds 1800 \
  --expected-sha256 "$EXPECTED_ISO_SHA256" \
  --expected-size "$EXPECTED_ISO_SIZE"
ISO="$SNAPSHOT_DIR/installer.iso"
chmod 0500 "$SNAPSHOT_DIR"
exec 9<"$ISO"

run python3 - "$ISO" "$MANIFEST" "$INSTALLER_PUBLIC_KEY" <<'PY'
from __future__ import annotations

import hashlib
import importlib.util
import json
import runpy
import shutil
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
if manifest.get("schema") != "drycontroller.os-installer-release.v3":
    raise SystemExit("manifest schema mismatch")
if not manifest.get("checks", {}).get("final_iso_release_checklist", {}).get("passed"):
    raise SystemExit("manifest does not record a passing final ISO checklist")
expected_sha = manifest.get("artifacts", {}).get("iso", {}).get("sha256")
actual_sha = sha256(iso)
if expected_sha != actual_sha:
    raise SystemExit(f"ISO sha256 mismatch: manifest={expected_sha} actual={actual_sha}")
if not manifest.get("release", {}).get("include_images") or not manifest.get("release", {}).get("public_material_only"):
    raise SystemExit("release manifest must include bundled Docker images")
public_scan = manifest.get("public_material_scan")
if not isinstance(public_scan, dict) or set(public_scan) != {
    "schema",
    "scanner_version",
    "policy_sha256",
    "report_sha256",
    "artifacts",
}:
    raise SystemExit("release manifest public-material evidence invalid")
scanner = runpy.run_path("scripts/public-material-scan.py")
if (
    public_scan.get("schema") != scanner["SCHEMA"]
    or public_scan.get("scanner_version") != scanner["SCANNER_VERSION"]
    or public_scan.get("policy_sha256") != scanner["POLICY_SHA256"]
):
    raise SystemExit("release manifest public-material policy mismatch")
scan_artifacts = public_scan.get("artifacts")
if not isinstance(scan_artifacts, dict) or set(scan_artifacts) != set(scanner["SURFACES"]):
    raise SystemExit("release manifest public-material evidence is incomplete")
if any(not isinstance(record, dict) or record.get("passed") is not True for record in scan_artifacts.values()):
    raise SystemExit("release manifest public-material evidence did not pass")
if (
    scan_artifacts["iso"].get("sha256") != actual_sha
    or scan_artifacts["filesystem"].get("iso_sha256") != actual_sha
):
    raise SystemExit("release manifest public-material evidence does not cover this ISO")
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


host_cosign = shutil.which("cosign")
if host_cosign is None:
    raise SystemExit("host cosign verifier unavailable")
maker.verify_final_iso(
    App(),
    iso,
    include_images=True,
    signature_verifier=Path(host_cosign),
)
print(f"manifest ok: {manifest_path}")
print(f"iso sha256: {actual_sha}")
PY

PUBLIC_SCAN_REPORT="$PUBLIC_SCAN_TMP_DIR/write-time-public-scan.json"
run python3 scripts/public-material-scan.py \
  --surfaces iso filesystem \
  --report "$PUBLIC_SCAN_REPORT" \
  --iso "$ISO"
run python3 - "$ISO" "$MANIFEST" "$PUBLIC_SCAN_REPORT" <<'PY'
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

iso = Path(sys.argv[1])
manifest = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
report = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))

digest = hashlib.sha256()
with iso.open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
iso_sha256 = digest.hexdigest()

signed = manifest["public_material_scan"]
if (
    report.get("passed") is not True
    or report.get("schema") != signed.get("schema")
    or report.get("scanner_version") != signed.get("scanner_version")
    or report.get("policy_sha256") != signed.get("policy_sha256")
):
    raise SystemExit("write-time public-material scan evidence invalid")
artifacts = report.get("artifacts")
if not isinstance(artifacts, dict) or set(artifacts) != {"iso", "filesystem"}:
    raise SystemExit("write-time public-material scan coverage invalid")
if (
    artifacts["iso"].get("passed") is not True
    or artifacts["filesystem"].get("passed") is not True
    or artifacts["iso"].get("sha256") != iso_sha256
    or artifacts["filesystem"].get("iso_sha256") != iso_sha256
):
    raise SystemExit("write-time public-material scan does not cover this ISO")
print("write-time ISO and filesystem public-material scan passed")
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
run python3 scripts/stable-release-artifact.py verify-fd \
  --fd 9 \
  --path "$ISO" \
  --expected-size "$EXPECTED_ISO_SIZE"
run diskutil unmountDisk force "$DISK"
log "/bin/dd if=/dev/fd/9 | sudo /bin/dd of=$RAW_DISK"
/bin/dd if=/dev/fd/9 bs=4m status=progress | sudo /bin/dd "of=$RAW_DISK" bs=4m
run sync
run sudo python3 - "$RAW_DISK" "$EXPECTED_ISO_SIZE" "$EXPECTED_ISO_SHA256" <<'PY'
import hashlib
import sys
from pathlib import Path

raw_disk = Path(sys.argv[1])
remaining = int(sys.argv[2])
expected = sys.argv[3]
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
log "write complete: $ISO_SOURCE -> $DISK"
