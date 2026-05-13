#!/bin/bash
# OTA 升级脚本（工业级：离线包校验/可回滚/非交互/就绪验证）
#
# 用法：
#   bash scripts/ota-update.sh online
#   bash scripts/ota-update.sh offline /opt/ota/1.2.0
#   bash scripts/ota-update.sh offline-tar /opt/ota/1.2.0.tar.gz
#   bash scripts/ota-update.sh rollback [backup_dir]
#
# 常用参数：
#   --yes                 非交互（默认开启：失败自动回滚）
#   --no-rollback         失败不回滚（不推荐）
#   --skip-ready          跳过 /ready 校验
#   --skip-smoke          跳过 scripts/smoke-test.sh
#   --backup-images       备份当前镜像为 tar（体积大，回滚更可靠）
#
# 说明：
# - 生产建议使用 TLS（MQTT_TLS=true）与版本化镜像标签；仅恢复 compose 文件并不能保证回滚到旧镜像。

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    log_error "缺少命令：$1"
    exit 1
  fi
}

# compose 兼容：优先 docker-compose
compose() {
  if command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    docker compose "$@"
  fi
}

sha256_check_dir() {
  local dir="$1"
  if [ ! -f "$dir/SHA256SUMS" ]; then
    log_warn "未找到 $dir/SHA256SUMS，跳过完整性校验（不推荐）"
    return 0
  fi
  log_info "验证 SHA256SUMS ..."
  (cd "$dir" && (
    if command -v sha256sum >/dev/null 2>&1; then
      sha256sum -c SHA256SUMS
    else
      shasum -a 256 -c SHA256SUMS
    fi
  ))
  log_info "SHA256 校验通过"
}

# 用 cosign 验证 compose 中所有镜像的签名（仅 online 模式调用——离线包不在 registry，cosign 无法 verify）
# 公钥查找顺序：${OTA_COSIGN_PUBKEY_HOST_PATH} -> /etc/drycontroller/keys/cosign.pub -> ${ROOT_DIR}/configs/keys/cosign.pub
verify_images_cosign() {
  local pubkey="${OTA_COSIGN_PUBKEY_HOST_PATH:-/etc/drycontroller/keys/cosign.pub}"
  if [ ! -f "$pubkey" ]; then
    pubkey="${ROOT_DIR}/configs/keys/cosign.pub"
  fi
  if [ ! -f "$pubkey" ]; then
    log_error "cosign 公钥未找到（尝试：\$OTA_COSIGN_PUBKEY_HOST_PATH、/etc/drycontroller/keys/cosign.pub、${ROOT_DIR}/configs/keys/cosign.pub）"
    return 1
  fi
  require_cmd cosign
  local images
  images="$(compose config 2>/dev/null | awk '/^[[:space:]]*image:/{print $2}' | sort -u || true)"
  if [ -z "$images" ]; then
    log_warn "未解析到 image 列表，跳过 cosign 验签"
    return 0
  fi
  log_info "使用 cosign 公钥：$pubkey"
  local failed=0
  while IFS= read -r img; do
    [ -z "$img" ] && continue
    log_info "cosign verify: $img"
    if ! cosign verify --key "$pubkey" "$img" >/dev/null 2>&1; then
      log_error "签名验证失败：$img"
      failed=1
    fi
  done <<< "$images"
  if [ $failed -ne 0 ]; then
    return 1
  fi
  log_info "✅ 所有镜像签名验证通过"
}

wait_http_ok() {
  local url="$1"
  local name="$2"
  local timeout_sec="$3"
  local start
  start="$(date +%s)"
  while true; do
    if curl -fsS "$url" >/dev/null 2>&1; then
      log_info "$name 就绪：$url"
      return 0
    fi
    local now
    now="$(date +%s)"
    if [ $((now - start)) -ge "$timeout_sec" ]; then
      log_error "$name 就绪超时：$url"
      return 1
    fi
    sleep 2
  done
}

LOCK_DIR="tmp/ota.lock"
acquire_lock() {
  mkdir -p tmp
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "$$" > "$LOCK_DIR/pid"
    return 0
  fi
  log_error "检测到 OTA 正在进行（存在 $LOCK_DIR），请确认无并发升级后再试。"
  exit 1
}
release_lock() {
  rm -rf "$LOCK_DIR" >/dev/null 2>&1 || true
}

usage() {
  sed -n '1,120p' "$0"
}

# 先处理 help（避免触发任何副作用）
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ] || [ $# -eq 0 ]; then
  usage
  exit 0
fi

MODE="$1"
shift

PKG=""
case "$MODE" in
  offline|offline-tar)
    PKG="${1:-}"
    shift || true
    ;;
  rollback)
    # rollback 的 backup_dir 可选；如果下一个参数不是 flag，则认为是目录
    if [ $# -gt 0 ] && [[ "${1:-}" != "-"* ]]; then
      PKG="$1"
      shift || true
    fi
    ;;
  online)
    ;;
  *)
    # 允许用户把 help 写在后面：scripts/ota-update.sh online --help
    if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
      usage
      exit 0
    fi
    ;;
esac

AUTO_YES=true
DO_ROLLBACK=true
SKIP_READY=false
SKIP_SMOKE=false
SKIP_PULL=false
BACKUP_IMAGES=false

while [ $# -gt 0 ]; do
  case "$1" in
    --yes) AUTO_YES=true ;;
    --no-rollback) DO_ROLLBACK=false ;;
    --skip-ready) SKIP_READY=true ;;
    --skip-smoke) SKIP_SMOKE=true ;;
    --skip-pull) SKIP_PULL=true ;;
    --backup-images) BACKUP_IMAGES=true ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      log_error "未知参数：$1"
      exit 1
      ;;
  esac
  shift
done

# 也允许通过环境变量控制（容器化联调更方便）
if [ "${OTA_SKIP_PULL:-}" != "" ]; then
  case "${OTA_SKIP_PULL}" in
    1|true|yes) SKIP_PULL=true ;;
    0|false|no) SKIP_PULL=false ;;
  esac
fi

BACKUP_DIR=""

backup_current_version() {
  log_info "备份当前版本..."
  mkdir -p backup
  BACKUP_DIR="backup/$(date +%Y%m%d_%H%M%S)"
  mkdir -p "$BACKUP_DIR"

  if [ -d configs ]; then
    cp -R configs "$BACKUP_DIR/" || true
  fi
  if [ -f .env ]; then
    cp .env "$BACKUP_DIR/" || true
  else
    log_warn "未找到 .env，跳过备份"
  fi
  if [ -f docker-compose.yml ]; then
    cp docker-compose.yml "$BACKUP_DIR/" || true
  fi

  compose config --services > "$BACKUP_DIR/services.txt" 2>/dev/null || true
  compose ps > "$BACKUP_DIR/ps.txt" 2>/dev/null || true
  docker images > "$BACKUP_DIR/docker-images.txt" 2>/dev/null || true

  if [ "$BACKUP_IMAGES" = true ]; then
    log_warn "启用镜像备份（会占用大量磁盘）..."
    mkdir -p "$BACKUP_DIR/images"
    # 从 compose 配置提取 image 列表
    local images
    images="$(compose config 2>/dev/null | awk '/^[[:space:]]*image:/{print $2}' | sort -u || true)"
    if [ -z "$images" ]; then
      log_warn "未解析到 image 列表，跳过镜像备份"
    else
      while IFS= read -r img; do
        [ -z "$img" ] && continue
        local safe
        safe="$(echo "$img" | tr '/:@' '___')"
        log_info "备份镜像：$img -> $BACKUP_DIR/images/$safe.tar"
        docker save -o "$BACKUP_DIR/images/$safe.tar" "$img"
      done <<< "$images"
    fi
  fi

  log_info "备份完成：$BACKUP_DIR"
}

apply_package_dir() {
  local dir="$1"
  log_info "应用 OTA 包目录：$dir"

  # 可选 pre-check
  if [ -f "$dir/pre-check.sh" ]; then
    log_info "执行 pre-check.sh ..."
    bash "$dir/pre-check.sh"
  fi

  sha256_check_dir "$dir"

  # 导入镜像
  if [ -d "$dir/images" ]; then
    local tars
    tars="$(ls -1 "$dir/images"/*.tar 2>/dev/null || true)"
    if [ -n "$tars" ]; then
      log_info "导入 Docker 镜像..."
      for f in $tars; do
        log_info "docker load: $(basename "$f")"
        docker load -i "$f"
      done
    else
      log_warn "未找到 images/*.tar，跳过镜像导入"
    fi
  fi

  # 更新 compose（可选）
  if [ -f "$dir/docker-compose.yml" ]; then
    log_info "更新 docker-compose.yml"
    cp "$dir/docker-compose.yml" docker-compose.yml
  fi

  # 更新配置（可选）
  if [ -d "$dir/configs" ]; then
    log_info "更新 configs/"
    mkdir -p configs
    cp -R "$dir/configs/"* configs/ || true
  fi

  # 更新 .env（可选：一般不建议 OTA 覆盖生产 .env，除非你们有明确约束）
  if [ -f "$dir/.env" ]; then
    log_warn "OTA 包包含 .env，将覆盖本地 .env（请确保符合现场流程）"
    cp "$dir/.env" .env
  fi

  # 重启服务（up 会滚动替换变更的容器）
  log_info "重启服务..."
  compose up -d --remove-orphans
  local rc=$?
  if [ $rc -ne 0 ]; then
    log_error "compose up 失败（rc=$rc）"
    return $rc
  fi

  # 可选 post-check
  if [ -f "$dir/post-check.sh" ]; then
    log_info "执行 post-check.sh ..."
    bash "$dir/post-check.sh"
  fi
}

verify_ready_and_smoke() {
  log_info "验证升级结果..."
  compose ps

  if [ "$SKIP_READY" = false ]; then
    # 默认宿主机探测：localhost 端口（edge-daq 默认映射到 8082，避免与 edge-monitor:8080 冲突）
    local api_port="${EDGE_API_PORT:-8000}"
    local daq_port="${EDGE_DAQ_HEALTH_PORT:-8082}"
    local api_ready_url="${EDGE_API_READY_URL:-http://localhost:${api_port}/ready}"
    local daq_ready_url="${EDGE_DAQ_READY_URL:-http://localhost:${daq_port}/ready}"
    wait_http_ok "$api_ready_url" "edge-api /ready" 60
    wait_http_ok "$daq_ready_url" "edge-daq /ready" 60
  else
    log_warn "已跳过 /ready 校验"
  fi

  if [ "$SKIP_SMOKE" = false ] && [ -f "scripts/smoke-test.sh" ]; then
    log_info "运行 smoke-test.sh ..."
    bash scripts/smoke-test.sh
  else
    log_warn "已跳过 smoke-test"
  fi
}

rollback_to() {
  local dir="${1:-}"
  if [ -z "$dir" ]; then
    if [ -d backup ]; then
      dir="backup/$(ls -t backup/ | head -1)"
    fi
  fi
  if [ -z "$dir" ] || [ ! -d "$dir" ]; then
    log_error "未找到可用备份目录"
    exit 1
  fi
  log_warn "回滚到：$dir"

  if [ -f "$dir/docker-compose.yml" ]; then
    cp "$dir/docker-compose.yml" docker-compose.yml
  fi
  if [ -f "$dir/.env" ]; then
    cp "$dir/.env" .env
  fi
  if [ -d "$dir/configs" ]; then
    mkdir -p configs
    cp -R "$dir/configs/"* configs/ || true
  fi

  # 可选：若备份带了 images/*.tar，可导入回滚镜像
  if [ -d "$dir/images" ]; then
    local tars
    tars="$(ls -1 "$dir/images"/*.tar 2>/dev/null || true)"
    if [ -n "$tars" ]; then
      log_info "导入备份镜像（增强回滚可靠性）..."
      for f in $tars; do
        log_info "docker load: $(basename "$f")"
        docker load -i "$f"
      done
    fi
  fi

  compose up -d --remove-orphans
  local rc=$?
  if [ $rc -ne 0 ]; then
    log_error "回滚 compose up 失败（rc=$rc）"
    return $rc
  fi
  verify_ready_and_smoke
  log_info "回滚完成"
}

main() {
  require_cmd docker
  require_cmd curl
  if ! command -v docker-compose >/dev/null 2>&1 && ! docker compose version >/dev/null 2>&1; then
    log_error "缺少 docker-compose 或 docker compose"
    exit 1
  fi

  if [ -z "${MODE:-}" ] || [ "$MODE" = "--help" ] || [ "$MODE" = "-h" ]; then
    usage
    exit 0
  fi

  case "$MODE" in
    online|rollback) ;;
    offline|offline-tar)
      if [ -z "${PKG:-}" ]; then
        log_error "缺少 OTA 包路径"
        usage
        exit 1
      fi
      ;;
    *)
      log_error "未知模式：$MODE"
      usage
      exit 1
      ;;
  esac

  acquire_lock
  trap release_lock EXIT

  log_info "OTA 模式：$MODE"
  backup_current_version

  set +e
  local ok=1
  local rc=0
  case "$MODE" in
    online)
      log_info "在线升级：docker pull + cosign verify + up -d"
      ok=0
      if [ "$SKIP_PULL" = false ]; then
        compose pull
        rc=$?
        if [ $rc -ne 0 ]; then
          log_error "compose pull 失败（rc=$rc）"
          ok=$rc
        fi
      else
        log_warn "已跳过 compose pull"
      fi
      if [ $ok -eq 0 ]; then
        verify_images_cosign
        rc=$?
        if [ $rc -ne 0 ]; then
          log_error "cosign 验签失败（rc=$rc）"
          ok=$rc
        fi
      fi
      if [ $ok -eq 0 ]; then
      compose up -d --remove-orphans
        rc=$?
        if [ $rc -ne 0 ]; then
          log_error "compose up 失败（rc=$rc）"
          ok=$rc
        fi
      fi
      if [ $ok -eq 0 ]; then
      verify_ready_and_smoke
        rc=$?
        if [ $rc -ne 0 ]; then
          log_error "验证失败（rc=$rc）"
          ok=$rc
        fi
      fi
      ;;
    offline)
      if [ -z "${PKG:-}" ] || [ ! -d "$PKG" ]; then
        log_error "离线升级需要提供 OTA 包目录：bash scripts/ota-update.sh offline /opt/ota/1.2.0"
        ok=2
      else
        apply_package_dir "$PKG"
        rc=$?
        if [ $rc -ne 0 ]; then ok=$rc; break; fi
        verify_ready_and_smoke
        rc=$?
        if [ $rc -ne 0 ]; then ok=$rc; break; fi
        ok=0
      fi
      ;;
    offline-tar)
      if [ -z "${PKG:-}" ] || [ ! -f "$PKG" ]; then
        log_error "离线 tar 包不存在：$PKG"
        ok=2
      else
        mkdir -p tmp
        local tmp_dir="tmp/ota_unpack_$(date +%Y%m%d_%H%M%S)"
        mkdir -p "$tmp_dir"
        log_info "解压 OTA 包：$PKG -> $tmp_dir"
        tar -xzf "$PKG" -C "$tmp_dir"
        apply_package_dir "$tmp_dir"
        rc=$?
        if [ $rc -ne 0 ]; then ok=$rc; rm -rf "$tmp_dir"; break; fi
        verify_ready_and_smoke
        rc=$?
        if [ $rc -ne 0 ]; then ok=$rc; rm -rf "$tmp_dir"; break; fi
        rm -rf "$tmp_dir"
        ok=0
      fi
      ;;
    rollback)
      rollback_to "${PKG:-}"
      ok=0
      ;;
    *)
      log_error "未知模式：$MODE"
      ok=2
      ;;
  esac
  set -e

  if [ $ok -ne 0 ]; then
    log_error "OTA 失败（mode=$MODE）"
    if [ "$DO_ROLLBACK" = true ]; then
      log_warn "开始自动回滚..."
      rollback_to "$BACKUP_DIR"
    else
      log_warn "已禁用自动回滚（--no-rollback）"
    fi
    exit 1
  fi

  log_info "✅ OTA 完成"
}

main

