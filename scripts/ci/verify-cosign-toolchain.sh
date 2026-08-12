#!/usr/bin/env bash
set -euo pipefail

COSIGN_APPROVED_VERSION="v3.1.3"
COSIGN_APPROVED_LINUX_AMD64_SHA256="4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71"

[ "$(uname -s)" = "Linux" ] || { echo "release cosign builder must be Linux" >&2; exit 1; }
[ "$(uname -m)" = "x86_64" ] || { echo "release cosign builder must be amd64" >&2; exit 1; }
command -v sha256sum >/dev/null
cosign_path="$(command -v cosign)"
[ -f "$cosign_path" ] && [ -x "$cosign_path" ] || { echo "release cosign binary unavailable" >&2; exit 1; }
actual_sha256="$(sha256sum "$cosign_path" | awk '{print $1}')"
[ "$actual_sha256" = "$COSIGN_APPROVED_LINUX_AMD64_SHA256" ] \
  || { echo "release cosign checksum is not approved" >&2; exit 1; }
version_output="$("$cosign_path" version 2>/dev/null)" \
  || { echo "release cosign version is unavailable" >&2; exit 1; }
grep -Eq "GitVersion:[[:space:]]+${COSIGN_APPROVED_VERSION//./\\.}([[:space:]]|$)" <<<"$version_output" \
  || { echo "release cosign version is not approved" >&2; exit 1; }
grep -Eq 'Platform:[[:space:]]+linux/amd64([[:space:]]|$)' <<<"$version_output" \
  || { echo "release cosign platform is not approved" >&2; exit 1; }
