#!/usr/bin/env bash
# Build the public, offline Python wheel set used by the TPM identity service.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUT_DIR="${REPO_ROOT}/dist/autoinstall/identity-wheelhouse"
[ $# -eq 0 ] || { echo "unexpected argument: $1" >&2; exit 2; }
[ "$(uname -s)" = "Linux" ] && [ "$(uname -m)" = "x86_64" ] \
  || { echo "identity wheelhouse requires the fixed Linux amd64 builder" >&2; exit 1; }
python3 - <<'PY'
import sys
if sys.version_info[:2] != (3, 12):
    raise SystemExit("identity wheelhouse requires Python 3.12")
PY
python3 -m pip --version >/dev/null 2>&1 \
  || { echo "python3-pip is required on the media builder" >&2; exit 1; }

temporary="$(mktemp -d)"
cleanup() {
  rm -rf "$temporary"
}
trap cleanup EXIT

python3 -m pip download \
  --disable-pip-version-check \
  --only-binary=:all: \
  --dest "$temporary" \
  -r "${REPO_ROOT}/services/device-identity/requirements.txt"
[ -n "$(find "$temporary" -maxdepth 1 -type f -name '*.whl' -print -quit)" ] \
  || { echo "identity wheelhouse is empty" >&2; exit 1; }

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
find "$temporary" -maxdepth 1 -type f -name '*.whl' -exec cp {} "$OUT_DIR/" \;
(
  cd "$OUT_DIR"
  sha256sum ./*.whl | LC_ALL=C sort -k2 > SHA256SUMS
)
chmod 0644 "$OUT_DIR"/*.whl "$OUT_DIR/SHA256SUMS"
echo "device identity wheelhouse written: $OUT_DIR"
