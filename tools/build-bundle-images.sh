#!/usr/bin/env bash
#
# 在 Mac/CI 上预构建边缘机全部 Docker 镜像，打包为 tarball 随 edge bundle 部署。
#
# 用途：
#   边缘机装机时无需访问网络——所有镜像以 docker save 格式打进 bundle，
#   bootstrap 直接 docker load 后 compose up，消除 GFW/DNS/pip 全部网络卡点。
#
# 产出：
#   dist/bundle-images.tar      — 全部 docker 镜像（供 bootstrap USB 加载）
#   dist/drycontroller-edge.tar.gz — edge 仓库 bundle（供 ISO 构建）
#
# 用法：
#   bash tools/build-bundle-images.sh              # 构建全部镜像
#   bash tools/build-bundle-images.sh --images-only # 仅构建镜像 tar
#   bash tools/build-bundle-images.sh --bundle-only # 仅打源码 bundle
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DIST_DIR="${REPO_ROOT}/dist"
COMPOSE_PROJECT="drycontroller-edge"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

BUILD_IMAGES=true
BUILD_BUNDLE=true

while [ $# -gt 0 ]; do
  case "$1" in
    --images-only) BUILD_BUNDLE=false; shift ;;
    --bundle-only) BUILD_IMAGES=false; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

mkdir -p "$DIST_DIR"

# ── 1. 预构建全部 Docker 镜像 ──────────────────────────────────────
if [ "$BUILD_IMAGES" = true ]; then
  log "building all service images (compose project: ${COMPOSE_PROJECT})..."

  cd "$REPO_ROOT"

  # 构建前快照
  BEFORE="${DIST_DIR}/.image-ids-before.txt"
  docker image ls --format '{{.Repository}}:{{.Tag}} {{.ID}}' | sort > "$BEFORE"

  # 构建所有 build-type 服务（含 profile: ota 的 edge-ota-agent）
  COMPOSE_PROJECT_NAME="$COMPOSE_PROJECT" COMPOSE_PROFILES="ota,remote-debug" docker compose build --parallel 2>&1 | \
    while IFS= read -r line; do log "  [build] $line"; done

  # 拉取 base images（含 profile: remote-debug 的 tailscale）
  COMPOSE_PROJECT_NAME="$COMPOSE_PROJECT" COMPOSE_PROFILES="remote-debug" docker compose pull --ignore-buildable 2>&1 | \
    while IFS= read -r line; do log "  [pull] $line"; done

  # 构建后 snapshot
  AFTER="${DIST_DIR}/.image-ids-after.txt"
  docker image ls --format '{{.Repository}}:{{.Tag}} {{.ID}}' | sort > "$AFTER"

  # 收集镜像：project 前缀（构建镜像）+ compose file 中声明的 image + 快照差异
  IMAGE_LIST="${DIST_DIR}/bundle-images.list"
  {
    docker image ls --format '{{.Repository}}:{{.Tag}}' | grep "^${COMPOSE_PROJECT}" || true
    grep -h 'image:' docker-compose.yml | sed 's/.*image:[[:space:]]*//' | tr -d '"' | sort -u
    comm -13 "$BEFORE" "$AFTER" | awk '{print $1}'
  } | sort -u > "$IMAGE_LIST"
  rm -f "$BEFORE" "$AFTER"

  COUNT="$(wc -l < "$IMAGE_LIST")"

  COUNT="$(wc -l < "$IMAGE_LIST")"
  if [ "$COUNT" -eq 0 ]; then
    die "no images collected; ensure docker compose build and pull completed"
  fi

  log "saving ${COUNT} images to ${DIST_DIR}/bundle-images.tar ..."
  xargs docker save -o "${DIST_DIR}/bundle-images.tar" < "$IMAGE_LIST"
  rm -f "$IMAGE_LIST"

  log "bundle-images.tar created ($(du -h "${DIST_DIR}/bundle-images.tar" | cut -f1))"
fi

# ── 2. 打包 edge 源码 bundle ──────────────────────────────────────
if [ "$BUILD_BUNDLE" = true ]; then
  BUNDLE="${DIST_DIR}/drycontroller-edge.tar.gz"

  log "packaging edge source bundle..."

  cd "$REPO_ROOT"
  tar -czf "$BUNDLE" \
    --exclude='.git' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='edge-local.env' \
    --exclude='infra/autoinstall/password.env' \
    --exclude='infra/autoinstall/wifi.env' \
    --exclude='.venv' \
    --exclude='.venv-*' \
    --exclude='.venv*' \
    --exclude='node_modules' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='.DS_Store' \
    --exclude='*.pyc' \
    --exclude='dist' \
    --exclude='logs' \
    --exclude='data' \
    --exclude='tmp' \
    --exclude='tools/os-installer-usb-maker/DryController USB Maker.app' \
    --exclude='tools/os-installer-usb-maker/build_app.sh' \
    --exclude='tools/os-installer-usb-maker/icon.icns' \
    .

  log "bundle created: $BUNDLE ($(du -h "$BUNDLE" | cut -f1))"

  # bundle-images.tar 与源码 bundle 分开存放，通过 post-install.sh 中的 tar -xzf
  # 解压后与源码一起部署到 /opt。镜像 tar 由 bootstrap 中 load_bootstrap_image_tars()
  # 从 USB 加载。
  if [ -f "${DIST_DIR}/bundle-images.tar" ]; then
    log "images tar: ${DIST_DIR}/bundle-images.tar ($(du -h "${DIST_DIR}/bundle-images.tar" | cut -f1))"
  fi
fi

log "done"
