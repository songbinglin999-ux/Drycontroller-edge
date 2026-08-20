#!/usr/bin/env bash
# Build the OS Installer apt repository from one fixed, local Ubuntu snapshot.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUT_DIR="${REPO_ROOT}/dist/autoinstall/drycontroller-apt"
SNAPSHOT_ROOT=""
LOCK_FILE=""
LOCK_SHA256=""
# The empty sentinel keeps Bash 3.2 + nounset portable for the optional array.
EXTRA_DEBS=("")
EXTRA_COUNT=0

usage() {
  cat <<'EOF'
Usage:
  bash scripts/build-autoinstall-offline-apt-repo.sh \
    --snapshot-root DIR --lock FILE --lock-sha256 SHA256 [--extra-deb FILE]

Builds the noble/amd64 OS Installer apt repository from a pre-staged Ubuntu
snapshot. The lock must bind one immutable snapshot URI, every Release and
Packages input, exact requested versions, and the complete resolved dependency
set. No mirror, current metadata, or online fallback is accepted.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --snapshot-root)
      SNAPSHOT_ROOT="${2:-}"
      [ -n "$SNAPSHOT_ROOT" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    --lock)
      LOCK_FILE="${2:-}"
      [ -n "$LOCK_FILE" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    --lock-sha256)
      LOCK_SHA256="${2:-}"
      [ -n "$LOCK_SHA256" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    --extra-deb)
      EXTRA_DEB="${2:-}"
      [ -n "$EXTRA_DEB" ] || { usage >&2; exit 2; }
      EXTRA_DEBS+=("$EXTRA_DEB")
      EXTRA_COUNT=$((EXTRA_COUNT + 1))
      shift 2
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

[ -n "$SNAPSHOT_ROOT" ] && [ -n "$LOCK_FILE" ] && [ -n "$LOCK_SHA256" ] || {
  usage >&2
  echo "--snapshot-root, --lock, and --lock-sha256 are required" >&2
  exit 2
}
[[ "$LOCK_SHA256" =~ ^[0-9a-f]{64}$ ]] || {
  echo "--lock-sha256 must be one lowercase SHA-256 digest" >&2
  exit 2
}

for command in apt-get dpkg-deb dpkg-scanpackages gzip python3 sha256sum; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "$command is required; run this on the fixed Ubuntu builder" >&2
    exit 1
  }
done
[ -d "$SNAPSHOT_ROOT" ] || { echo "snapshot root not found: $SNAPSHOT_ROOT" >&2; exit 1; }
[ -f "$LOCK_FILE" ] || { echo "offline apt input lock not found: $LOCK_FILE" >&2; exit 1; }

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

PLAN="${TMP_DIR}/validated-plan.json"
COMBINED_PACKAGES="${TMP_DIR}/validated-Packages"
python3 "${SCRIPT_DIR}/validate-autoinstall-offline-apt-input.py" validate \
  --snapshot-root "$SNAPSHOT_ROOT" \
  --lock "$LOCK_FILE" \
  --lock-sha256 "$LOCK_SHA256" \
  --plan-out "$PLAN" \
  --combined-packages-out "$COMBINED_PACKAGES"

EXTRA_REPO="${TMP_DIR}/extra-repo"
mkdir -p "$EXTRA_REPO/pool"
for deb in "${EXTRA_DEBS[@]}"; do
  [ -n "$deb" ] || continue
  case "$(basename "$deb")" in
    *.deb) ;;
    *) echo "extra DEB filename must end in .deb: $deb" >&2; exit 1 ;;
  esac
  [ -f "$deb" ] && [ ! -L "$deb" ] || {
    echo "extra DEB must be a regular non-symlink file: $deb" >&2
    exit 1
  }
  target="$EXTRA_REPO/pool/$(basename "$deb")"
  [ ! -e "$target" ] || { echo "extra DEB filenames collide" >&2; exit 1; }
  input_digest="$(sha256sum "$deb" | awk '{print $1}')"
  install -m 0644 "$deb" "$target"
  [ "$(sha256sum "$target" | awk '{print $1}')" = "$input_digest" ] || {
    echo "extra DEB changed while copying: $deb" >&2
    exit 1
  }
  extra_arch="$(dpkg-deb --field "$target" Architecture)"
  [ "$extra_arch" = "amd64" ] || [ "$extra_arch" = "all" ] || {
    echo "extra DEB architecture is not amd64/all: $deb" >&2
    exit 1
  }
done
EXTRA_PACKAGES="${TMP_DIR}/extra-Packages"
if [ "$EXTRA_COUNT" -gt 0 ]; then
  (
    cd "$EXTRA_REPO"
    dpkg-scanpackages pool /dev/null > "$EXTRA_PACKAGES"
  )
else
  : > "$EXTRA_PACKAGES"
fi
SOLVER_PLAN="${TMP_DIR}/solver-plan.json"
python3 - "$PLAN" "$EXTRA_REPO" "$SOLVER_PLAN" <<'PY'
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

plan_path, extra_repo, output_path = map(Path, sys.argv[1:])
plan = json.loads(plan_path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def deb_field(path: Path, field: str) -> str:
    return subprocess.check_output(
        ["dpkg-deb", "--field", str(path), field], text=True
    ).strip()


extra_records = []
known_names = {item["name"] for item in plan["resolved_packages"]}
for path in sorted((extra_repo / "pool").glob("*.deb")):
    name = deb_field(path, "Package")
    version = deb_field(path, "Version")
    architecture = deb_field(path, "Architecture")
    if (
        re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name) is None
        or re.fullmatch(r"[A-Za-z0-9.+:~\-]+", version) is None
        or architecture not in {"amd64", "all"}
        or name in known_names
    ):
        raise SystemExit("extra DEB metadata conflicts with the locked package set")
    known_names.add(name)
    extra_records.append(
        {
            "name": name,
            "version": version,
            "architecture": architecture,
            "filename": f"pool/{path.name}",
            "size": path.stat().st_size,
            "sha256": sha256(path),
        }
    )

solver_plan = {
    **plan,
    "requested_packages": [
        *plan["requested_packages"],
        *(
            {"name": item["name"], "version": item["version"]}
            for item in extra_records
        ),
    ],
    "resolved_packages": [*plan["resolved_packages"], *extra_records],
    "extra_debs": extra_records,
}
output_path.write_text(
    json.dumps(solver_plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    + "\n",
    encoding="utf-8",
)
PY

SOLVER_REPO="${TMP_DIR}/solver-repo"
mkdir -p "$SOLVER_REPO"
install -m 0644 "$COMBINED_PACKAGES" "$SOLVER_REPO/Packages"
if [ "$EXTRA_COUNT" -gt 0 ]; then
  printf '\n' >> "$SOLVER_REPO/Packages"
  cat "$EXTRA_PACKAGES" >> "$SOLVER_REPO/Packages"
fi
gzip -n -9c "$SOLVER_REPO/Packages" > "$SOLVER_REPO/Packages.gz"
SOLVER_URI="$(python3 - "$SOLVER_REPO" <<'PY'
import sys
from pathlib import Path

print(Path(sys.argv[1]).resolve().as_uri())
PY
)"
mkdir -p \
  "${TMP_DIR}/apt-state/lists/partial" \
  "${TMP_DIR}/apt-cache/archives/partial" \
  "${TMP_DIR}/apt-etc/sourceparts" \
  "${TMP_DIR}/apt-etc/preferencesparts"
touch "${TMP_DIR}/apt-state/status"
printf 'deb [trusted=yes arch=amd64] %s ./\n' "$SOLVER_URI" > "${TMP_DIR}/apt-etc/sources.list"

APT_OPTS=(
  -o "Dir::State::status=${TMP_DIR}/apt-state/status"
  -o "Dir::State::lists=${TMP_DIR}/apt-state/lists"
  -o "Dir::Cache=${TMP_DIR}/apt-cache"
  -o "Dir::Cache::archives=${TMP_DIR}/apt-cache/archives"
  -o "Dir::Etc::sourcelist=${TMP_DIR}/apt-etc/sources.list"
  -o "Dir::Etc::sourceparts=${TMP_DIR}/apt-etc/sourceparts"
  -o "Dir::Etc::main=/dev/null"
  -o "Dir::Etc::trusted=/dev/null"
  -o "Dir::Etc::trustedparts=/dev/null"
  -o "Dir::Etc::parts=/dev/null"
  -o "Dir::Etc::preferences=/dev/null"
  -o "Dir::Etc::preferencesparts=${TMP_DIR}/apt-etc/preferencesparts"
  -o Debug::NoLocking=1
  -o APT::Sandbox::User=root
  -o Acquire::Languages=none
  -o Acquire::Check-Valid-Until=false
  -o APT::Architecture=amd64
)

echo "Loading fixed local apt metadata..."
APT_CONFIG=/dev/null LC_ALL=C apt-get "${APT_OPTS[@]}" update >/dev/null
REQUESTED_SPECS=()
while IFS= read -r requested_spec; do
  REQUESTED_SPECS+=("$requested_spec")
done < <(python3 - "$SOLVER_PLAN" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for item in plan["requested_packages"]:
    print(f"{item['name']}={item['version']}")
PY
)
[ "${#REQUESTED_SPECS[@]}" -gt 0 ] || {
  echo "validated apt plan has no requested packages" >&2
  exit 1
}
SOLVER_OUTPUT="${TMP_DIR}/solver-output.txt"
echo "Verifying the exact dependency closure..."
if ! APT_CONFIG=/dev/null LC_ALL=C apt-get \
  "${APT_OPTS[@]}" \
  --simulate \
  --no-install-recommends \
  -y install "${REQUESTED_SPECS[@]}" > "$SOLVER_OUTPUT" 2>&1; then
  echo "apt could not resolve the locked package versions from fixed metadata" >&2
  exit 1
fi
python3 "${SCRIPT_DIR}/validate-autoinstall-offline-apt-input.py" verify-solver \
  --plan "$SOLVER_PLAN" \
  --solver-output "$SOLVER_OUTPUT"

# Revalidate every input after dependency resolution. The copy below is also
# checked against each locked digest, closing path replacement during the run.
PLAN_AFTER="${TMP_DIR}/validated-plan-after.json"
PACKAGES_AFTER="${TMP_DIR}/validated-Packages-after"
python3 "${SCRIPT_DIR}/validate-autoinstall-offline-apt-input.py" validate \
  --snapshot-root "$SNAPSHOT_ROOT" \
  --lock "$LOCK_FILE" \
  --lock-sha256 "$LOCK_SHA256" \
  --plan-out "$PLAN_AFTER" \
  --combined-packages-out "$PACKAGES_AFTER"
cmp -s "$PLAN" "$PLAN_AFTER" && cmp -s "$COMBINED_PACKAGES" "$PACKAGES_AFTER" || {
  echo "offline apt input changed during dependency resolution" >&2
  exit 1
}

STAGE_DIR="${TMP_DIR}/drycontroller-apt"
mkdir -p "$STAGE_DIR/pool"
install -m 0644 "$LOCK_FILE" "$STAGE_DIR/input-lock.json"
[ "$(sha256sum "$STAGE_DIR/input-lock.json" | awk '{print $1}')" = "$LOCK_SHA256" ] || {
  echo "offline apt input lock changed while copying" >&2
  exit 1
}

while IFS=$'\t' read -r package version architecture filename size digest; do
  source_path="${SNAPSHOT_ROOT}/${filename}"
  target_path="${STAGE_DIR}/${filename}"
  mkdir -p "$(dirname "$target_path")"
  [ ! -e "$target_path" ] || { echo "locked package paths collide" >&2; exit 1; }
  install -m 0644 "$source_path" "$target_path"
  [ "$(sha256sum "$target_path" | awk '{print $1}')" = "$digest" ] || {
    echo "locked package changed while copying: $package" >&2
    exit 1
  }
  [ "$(wc -c < "$target_path" | tr -d ' ')" = "$size" ] || {
    echo "locked package size changed while copying: $package" >&2
    exit 1
  }
  [ "$(dpkg-deb --field "$target_path" Package)" = "$package" ] \
    && [ "$(dpkg-deb --field "$target_path" Version)" = "$version" ] \
    && [ "$(dpkg-deb --field "$target_path" Architecture)" = "$architecture" ] || {
      echo "locked package DEB metadata mismatch: $package" >&2
      exit 1
    }
done < <(python3 - "$PLAN" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for item in plan["resolved_packages"]:
    print(
        "\t".join(
            str(item[key])
            for key in ("name", "version", "architecture", "filename", "size", "sha256")
        )
    )
PY
)

while IFS=$'\t' read -r package version architecture filename size digest; do
  source_path="${EXTRA_REPO}/${filename}"
  target_path="${STAGE_DIR}/${filename}"
  [ ! -e "$target_path" ] || { echo "extra DEB path collides with locked pool" >&2; exit 1; }
  install -m 0644 "$source_path" "$target_path"
  [ "$(sha256sum "$target_path" | awk '{print $1}')" = "$digest" ] \
    && [ "$(wc -c < "$target_path" | tr -d ' ')" = "$size" ] \
    && [ "$(dpkg-deb --field "$target_path" Package)" = "$package" ] \
    && [ "$(dpkg-deb --field "$target_path" Version)" = "$version" ] \
    && [ "$(dpkg-deb --field "$target_path" Architecture)" = "$architecture" ] || {
      echo "extra DEB changed after dependency resolution: $package" >&2
      exit 1
    }
done < <(python3 - "$SOLVER_PLAN" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for item in plan["extra_debs"]:
    print(
        "\t".join(
            str(item[key])
            for key in ("name", "version", "architecture", "filename", "size", "sha256")
        )
    )
PY
)

(
  cd "$STAGE_DIR"
  dpkg-scanpackages pool /dev/null > Packages
  gzip -n -9c Packages > Packages.gz
)

python3 - "$PLAN" "$SOLVER_PLAN" "$STAGE_DIR" <<'PY'
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

plan_path = Path(sys.argv[1])
solver_plan_path = Path(sys.argv[2])
output = Path(sys.argv[3])
plan = json.loads(plan_path.read_text(encoding="utf-8"))
solver_plan = json.loads(solver_plan_path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deb_field(path: Path, field: str) -> str:
    return subprocess.check_output(
        ["dpkg-deb", "--field", str(path), field], text=True
    ).strip()


def packages_inventory(path: Path) -> list[dict[str, object]]:
    required = ("Package", "Version", "Architecture", "Filename", "Size", "SHA256")
    records = []
    for paragraph in path.read_text(encoding="utf-8").strip().split("\n\n"):
        fields = {}
        for line in paragraph.splitlines():
            if line.startswith((" ", "\t")):
                continue
            key, separator, value = line.partition(":")
            if separator:
                fields[key] = value.lstrip()
        if any(field not in fields for field in required):
            raise SystemExit("generated offline apt Packages entry is incomplete")
        try:
            size = int(fields["Size"])
        except ValueError as exc:
            raise SystemExit("generated offline apt Packages size is invalid") from exc
        records.append(
            {
                "name": fields["Package"],
                "version": fields["Version"],
                "architecture": fields["Architecture"],
                "filename": fields["Filename"],
                "size": size,
                "sha256": fields["SHA256"],
            }
        )
    return sorted(records, key=lambda item: item["filename"])


resolved_paths = {item["filename"] for item in plan["resolved_packages"]}
pool = []
extra_debs = []
for path in sorted((output / "pool").rglob("*.deb")):
    relative = path.relative_to(output).as_posix()
    record = {
        "name": deb_field(path, "Package"),
        "version": deb_field(path, "Version"),
        "architecture": deb_field(path, "Architecture"),
        "filename": relative,
        "size": path.stat().st_size,
        "sha256": sha256(path),
    }
    pool.append(record)
    if relative not in resolved_paths:
        extra_debs.append(record)

pool.sort(key=lambda item: item["filename"])
extra_debs.sort(key=lambda item: item["filename"])
if packages_inventory(output / "Packages") != pool:
    raise SystemExit("generated offline apt Packages inventory differs from copied DEBs")
expected_resolved = sorted(plan["resolved_packages"], key=lambda item: item["filename"])
actual_resolved = [item for item in pool if item["filename"] in resolved_paths]
if actual_resolved != expected_resolved:
    raise SystemExit("copied offline apt package inventory differs from resolved lock")
if extra_debs != sorted(solver_plan["extra_debs"], key=lambda item: item["filename"]):
    raise SystemExit("copied extra DEB inventory differs from solver input")

receipt = {
    "schema": "drycontroller.offline-apt-receipt.v1",
    "snapshot_uri": plan["snapshot_uri"],
    "suite": plan["suite"],
    "architecture": plan["architecture"],
    "lock_sha256": plan["lock_sha256"],
    "release_files": plan["release_files"],
    "package_indexes": plan["package_indexes"],
    "requested_packages": plan["requested_packages"],
    "resolved_packages": plan["resolved_packages"],
    "extra_debs": extra_debs,
    "solver": {
        "requested_packages": solver_plan["requested_packages"],
        "resolved_packages": solver_plan["resolved_packages"],
    },
    "output": {
        "packages_sha256": sha256(output / "Packages"),
        "packages_gzip_sha256": sha256(output / "Packages.gz"),
        "pool": pool,
    },
}
(output / "receipt.json").write_text(
    json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY

mkdir -p "$(dirname "$OUT_DIR")"
rm -rf "$OUT_DIR"
mv "$STAGE_DIR" "$OUT_DIR"

cat <<EOF
Replayable offline apt repository written to:
  ${OUT_DIR}

Inputs were consumed only from the locked local snapshot:
  lock_sha256=${LOCK_SHA256}
EOF
