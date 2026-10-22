#!/bin/bash
# Optional: verify that cross-repo schemas in common/schemas/ are byte-identical
# to the copy kept by a companion cloud server (separate project, not included).
#
# The edge runtime is self-contained: common/schemas/ is vendored in this repo.
# This check is only useful if you also run a cloud server that mirrors
# cloud_to_edge.py.
#
# Usage:
#   PEER_SCHEMA_RAW_BASE_URL=https://git.example.com/raw/cloud/main/app/common/schemas \
#   [PEER_SCHEMA_TOKEN=...] bash scripts/check-shared-schemas-sync.sh
#
# Without PEER_SCHEMA_RAW_BASE_URL the check is skipped (exit 0).
# Exit codes: 0 = in sync or skipped, 1 = drift / fetch failure.

set -euo pipefail

if [ -z "${PEER_SCHEMA_RAW_BASE_URL:-}" ]; then
  echo "PEER_SCHEMA_RAW_BASE_URL not set; schema parity check skipped."
  exit 0
fi

EDGE_SCHEMA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/common/schemas"
IFS=',' read -ra CROSS_REPO_FILES <<< "$(echo "${CROSS_REPO_FILES:-cloud_to_edge.py}" | tr -d ' ')"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

curl_args=(-fsS --max-time 30)
if [ -n "${PEER_SCHEMA_TOKEN:-}" ]; then
  curl_args+=(-H "Authorization: token ${PEER_SCHEMA_TOKEN}")
fi

failed=0
for f in "${CROSS_REPO_FILES[@]}"; do
  edge="$EDGE_SCHEMA_DIR/$f"
  peer="$TMP_DIR/$f"
  url="${PEER_SCHEMA_RAW_BASE_URL%/}/${f}"
  echo "==> fetch $f from $url"
  if ! curl "${curl_args[@]}" -o "$peer" "$url"; then
    echo "failed to fetch $f"
    failed=1
    continue
  fi
  if [ ! -f "$edge" ]; then
    echo "$f missing on edge side ($edge)"
    failed=1
  elif diff -q "$edge" "$peer" >/dev/null; then
    echo "$f byte-identical"
  else
    echo "$f drifted:"
    diff "$edge" "$peer" | head -30 || true
    failed=1
  fi
done

if [ $failed -ne 0 ]; then
  echo "Schema parity check FAILED."
  exit 1
fi
echo "All cross-repo schemas in sync."
