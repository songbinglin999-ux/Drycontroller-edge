#!/usr/bin/env bash
#
# Build a flat apt repository for packages that must be available from the
# OS Installer USB when factory/site networks cannot reach Ubuntu mirrors.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUT_DIR="${REPO_ROOT}/dist/autoinstall/drycontroller-apt"
SUITE=""
ARCH=""
MIRROR=""
EXTRA_DEBS=()
PACKAGES=(
  ca-certificates
  curl
  python3
  tar
  iproute2
  lsb-release
  docker.io
  network-manager
)

usage() {
  cat <<'EOF'
Usage:
  bash scripts/build-autoinstall-offline-apt-repo.sh [--out DIR] [--suite CODENAME] [--arch ARCH] [--mirror URI] [--extra-deb FILE]

Builds a flat apt repository at DIR, suitable for copying to the root of the
Ubuntu Server OS Installer USB as:

  /drycontroller-apt/

Run this on an Ubuntu/Debian amd64 build host whose apt sources match the target
installer ISO release. Current release media is Ubuntu 24.04 LTS, so build on
noble or pass --suite noble.

When the default Ubuntu archive is unstable, pass a reachable mirror, for example:

  --mirror https://mirrors.aliyun.com/ubuntu/
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --out)
      OUT_DIR="${2:-}"
      [ -n "$OUT_DIR" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    --suite)
      SUITE="${2:-}"
      [ -n "$SUITE" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    --arch)
      ARCH="${2:-}"
      [ -n "$ARCH" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    --mirror)
      MIRROR="${2:-}"
      [ -n "$MIRROR" ] || { usage >&2; exit 2; }
      shift 2
      ;;
    --extra-deb)
      EXTRA_DEB="${2:-}"
      [ -n "$EXTRA_DEB" ] || { usage >&2; exit 2; }
      EXTRA_DEBS+=("$EXTRA_DEB")
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

command -v apt-get >/dev/null 2>&1 || {
  echo "apt-get is required; run this on an Ubuntu/Debian build host" >&2
  exit 1
}
command -v dpkg-scanpackages >/dev/null 2>&1 || {
  echo "dpkg-scanpackages is required; install dpkg-dev first" >&2
  exit 1
}

if [ -z "$ARCH" ]; then
  ARCH="$(dpkg --print-architecture)"
fi
[ "$ARCH" = "amd64" ] || {
  echo "unsupported architecture: ${ARCH}; current OS Installer USB targets amd64 IPCs" >&2
  exit 1
}

if [ -z "$SUITE" ] && [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  SUITE="${VERSION_CODENAME:-}"
fi
[ -n "$SUITE" ] || {
  echo "could not detect suite; pass --suite noble for current Ubuntu 24.04 media" >&2
  exit 1
}

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

rm -rf "${OUT_DIR}/pool"
mkdir -p "${OUT_DIR}/pool"
rm -f "${OUT_DIR}/Packages" "${OUT_DIR}/Packages.gz" "${OUT_DIR}/drycontroller-apt-manifest.txt"
touch "${TMP_DIR}/status"
mkdir -p "${TMP_DIR}/apt-cache/archives/partial"

APT_OPTS=(
  -o "Dir::State::status=${TMP_DIR}/status"
  -o "Dir::Cache=${TMP_DIR}/apt-cache"
  -o "Dir::Cache::archives=${TMP_DIR}/apt-cache/archives"
  -o Debug::NoLocking=1
  -o APT::Sandbox::User=root
)

if [ -n "$MIRROR" ]; then
  MIRROR="${MIRROR%/}"
  cat > "${TMP_DIR}/sources.list" <<EOF
deb ${MIRROR}/ ${SUITE} main restricted universe multiverse
deb ${MIRROR}/ ${SUITE}-updates main restricted universe multiverse
deb ${MIRROR}/ ${SUITE}-security main restricted universe multiverse
EOF
  APT_OPTS+=(
    -o "Dir::Etc::sourcelist=${TMP_DIR}/sources.list"
    -o "Dir::Etc::sourceparts=${TMP_DIR}/sources.list.d"
    -o "Dir::Etc::main=/dev/null"
  )
  mkdir -p "${TMP_DIR}/sources.list.d"
fi

echo "Updating apt metadata for ${SUITE}/${ARCH}..."
apt-get "${APT_OPTS[@]}" update

if apt-cache "${APT_OPTS[@]}" show docker-compose-v2 >/dev/null 2>&1; then
  PACKAGES+=(docker-compose-v2)
elif apt-cache "${APT_OPTS[@]}" show docker-compose-plugin >/dev/null 2>&1; then
  PACKAGES+=(docker-compose-plugin)
else
  echo "no Docker Compose v2 package found in current apt sources" >&2
  exit 1
fi

echo "Downloading packages and dependencies..."
apt-get \
  "${APT_OPTS[@]}" \
  --download-only \
  --no-install-recommends \
  --reinstall \
  -y install "${PACKAGES[@]}"

find "${TMP_DIR}/apt-cache/archives" -maxdepth 1 -type f -name '*.deb' -exec cp -f {} "${OUT_DIR}/pool/" \;
for deb in "${EXTRA_DEBS[@]}"; do
  [ -f "$deb" ] || {
    echo "extra deb not found: ${deb}" >&2
    exit 1
  }
  cp -f "$deb" "${OUT_DIR}/pool/"
done
DEB_COUNT="$(find "${OUT_DIR}/pool" -maxdepth 1 -type f -name '*.deb' | wc -l | tr -d ' ')"
[ "$DEB_COUNT" -gt 0 ] || {
  echo "no .deb files were downloaded" >&2
  exit 1
}

(
  cd "$OUT_DIR"
  dpkg-scanpackages --arch "$ARCH" pool /dev/null > Packages
  gzip -9c Packages > Packages.gz
  {
    echo "suite=${SUITE}"
    echo "arch=${ARCH}"
    printf 'packages='
    printf '%s ' "${PACKAGES[@]}"
    echo
    if [ "${#EXTRA_DEBS[@]}" -gt 0 ]; then
      printf 'extra_debs='
      for deb in "${EXTRA_DEBS[@]}"; do
        printf '%s ' "$(basename "$deb")"
      done
      echo
    fi
    date -u '+created_at=%Y-%m-%dT%H:%M:%SZ'
  } > drycontroller-apt-manifest.txt
)

cat <<EOF
Offline apt repository written to:
  ${OUT_DIR}

Copy this directory to the OS Installer USB root as:
  drycontroller-apt/

The installed target will use:
  deb [trusted=yes] file:/opt/drycontroller-apt ./
EOF
