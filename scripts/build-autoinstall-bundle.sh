#!/usr/bin/env bash
# Build the public, non-secret autoinstall overlay. This never writes a disk.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUT_DIR="${REPO_ROOT}/dist/autoinstall"

if [ "${1:-}" = "--out" ]; then
  [ -n "${2:-}" ] || { echo "--out requires a directory" >&2; exit 2; }
  OUT_DIR="$2"
  shift 2
fi
[ $# -eq 0 ] || { echo "unexpected argument: $1" >&2; exit 2; }

mkdir -p "$OUT_DIR"
install -m 0644 "${REPO_ROOT}/infra/autoinstall/user-data" "${OUT_DIR}/user-data"
python3 - "$OUT_DIR/user-data" <<'PY'
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
required = (
    'password: "!"',
    "allow-pw: false",
    "http://127.0.0.1:18080",
    "python3 -m http.server 18080",
    "sources_list:",
    "disable_suites:",
    "/cdrom/host-release-seed.json",
    "/cdrom/software-release-seed.json",
)
missing = [item for item in required if item not in text]
if missing:
    raise SystemExit("user-data public installer contract missing: " + ", ".join(missing))
for forbidden in (
    "REPLACE_WITH_SHA512_CRYPT_HASH",
    "mirrors.aliyun.com",
    "mirrors.tuna.tsinghua.edu.cn",
    "archive.ubuntu.com",
    "security.ubuntu.com",
    "wifi.env",
    "activation-bundle.json",
):
    if forbidden in text:
        raise SystemExit(f"user-data contains forbidden installer material: {forbidden}")
PY
install -m 0644 "${REPO_ROOT}/infra/autoinstall/meta-data" "${OUT_DIR}/meta-data"
install -m 0755 "${REPO_ROOT}/infra/autoinstall/post-install.sh" "${OUT_DIR}/post-install.sh"
install -m 0644 "${REPO_ROOT}/infra/autoinstall/drycontroller-edge-post-install.service" "${OUT_DIR}/drycontroller-edge-post-install.service"

BUNDLE_SCRIPT="${REPO_ROOT}/tools/build-bundle-images.sh"
BUNDLE_SRC="${REPO_ROOT}/dist/drycontroller-edge.tar.gz"
if [ ! -f "$BUNDLE_SRC" ]; then
  bash "$BUNDLE_SCRIPT" --bundle-only
fi
cp "$BUNDLE_SRC" "${OUT_DIR}/drycontroller-edge.tar.gz"

echo "public autoinstall overlay written: $OUT_DIR"
