#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'EOF'
OS Installer release builds are fail-closed on the RB-5A branch.

RB-5A removes the retired shared factory credential path but does not produce
production media. Re-enable this wrapper only in RB-6, after RB-5B supplies the
reviewed TPM identity backend and the media contains public material only.
EOF
exit 1
