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

log() { printf '[%s] %s\n' "$(date -Is)" "$*"; }
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

  # 构建 build-type 服务（edge-api, edge-daq, edge-monitor, ai-*, license-service, edge-ota-agent）
  COMPOSE_PROJECT_NAME="$COMPOSE_PROJECT" docker compose build --parallel 2>&1 | \
    while IFS= read -r line; do log "  [build] $line"; done

  # 拉取 base images（tailscale, postgres, influxdb, mosquitto, prometheus, alertmanager, socket-proxy）
  COMPOSE_PROJECT_NAME="$COMPOSE_PROJECT" docker compose pull --ignore-buildable 2>&1 | \
    while IFS= read -r line; do log "  [pull] $line"; done

  # 收集需要保存的镜像列表
  IMAGE_LIST="${DIST_DIR}/bundle-images.list"
  {
    # compose 构建的服务镜像
    docker image ls --format '{{.Repository}}:{{.Tag}}' | grep "^${COMPOSE_PROJECT}_" || true
    # compose 拉取的 base 镜像
    docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -E '^(tailscale/tailscale|tecnativa/docker-socket-proxy|postgres|influxdb|eclipse-mosquitto|prom/prometheus|prom/alertmanager):' || true
  } | sort -u > "$IMAGE_LIST"

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
    --exclude='.venv' \
    --exclude='.venv-*' \
    --exclude='node_modules' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='*.pyc' \
    --exclude='dist/*.iso' \
    --exclude='dist/*.img' \
    --exclude='logs' \
    --exclude='tools/os-installer-usb-maker/DryController USB Maker.app' \
    --exclude='tools/os-installer-usb-maker/build_app.sh' \
    --exclude='tools/os-installer-usb-maker/icon.icns' \
    .

  log "bundle created: $BUNDLE ($(du -h "$BUNDLE" | cut -f1))"

  # 把镜像 tar 也打进 bundle（如果存在）
  if [ -f "${DIST_DIR}/bundle-images.tar" ]; then
    log "appending bundle-images.tar to source bundle..."
    cp "$BUNDLE" "${BUNDLE}.tmp"
    tar -rf "${BUNDLE}.tmp" -C "$DIST_DIR" bundle-images.tar 2>/dev/null || {
      # tar 追加对大文件不太可靠，回退到单独存放
      log "tar append failed; keeping bundle-images.tar as separate file"
      rm -f "${BUNDLE}.tmp"
    }
    if [ -f "${BUNDLE}.tmp" ]; then
      gzip -f "${BUNDLE}.tmp"
      mv "${BUNDLE}.tmp.gz" "$BUNDLE"
      log "bundle with images: $BUNDLE ($(du -h "$BUNDLE" | cut -f1))"
    fi
  fi
fi

log "done"
