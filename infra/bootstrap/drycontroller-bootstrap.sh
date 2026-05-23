#!/usr/bin/env bash
#
# Drycontroller 一次性装机引导（开机自动执行）
# - 目的：装机人员只需要“插 U 盘 + 重启”，无需命令行。
# - 逻辑：扫描 U 盘上的 provisioning 包 -> 写入运维资产 -> docker login -> 启动 tailscale/enroll/核心服务 -> 云端 license 绑定 -> 标记完成。
#
# U 盘目录要求（任意挂载点下）：
#   drycontroller-provisioning/provisioning.json
#   drycontroller-provisioning/cosign.pub
#
# 说明：
# - 本脚本应由 systemd 作为 oneshot service 调用。
# - 需要宿主机安装 docker + docker compose v2 或 docker-compose。
#
set -euo pipefail

LOG_FILE="/var/log/drycontroller-bootstrap.log"
STATE_DIR="/var/lib/drycontroller"
SENTINEL="${STATE_DIR}/.bootstrapped"
STATUS_FILE="${STATE_DIR}/provisioning.status"
ALLOCATION_CACHE_FILE="${STATE_DIR}/allocation-response.json"
ASSET_DIR="/etc/drycontroller"
KEYS_DIR="${ASSET_DIR}/keys"
LICENSE_DIR="${ASSET_DIR}/license"

BOOTSTRAP_STARTED_AT="$(date -Is)"
BOOTSTRAP_FAIL_REASON=""
BOOTSTRAP_USB_MOUNT="/mnt/drycontroller-provisioning"

# ── ANSI 颜色 ──────────────────────────────────────────────────────
C_RESET='\033[0m'
C_BOLD='\033[1m'
C_RED='\033[31m'
C_GREEN='\033[32m'
C_YELLOW='\033[33m'
C_CYAN='\033[36m'
C_BG_GREEN='\033[42m'
C_BG_RED='\033[41m'

_has_color() {
  [ -t 2 ] || [ -w /dev/console ] && return 0 || return 1
}

_plain() { printf '%s\n' "$*"; }
_color() {
  local color="$1"; shift
  if _has_color; then
    printf "%s%s%s\n" "$color" "$*" "$C_RESET"
  else
    printf '%s\n' "$*"
  fi
}

mkdir -p "$(dirname "$LOG_FILE")" "$STATE_DIR" "$KEYS_DIR" "$LICENSE_DIR"
chmod 700 "$STATE_DIR" || true
chmod 755 "$ASSET_DIR" "$KEYS_DIR" || true
chmod 750 "$LICENSE_DIR" || true

log()  { echo "[$(date -Is)] $*" | tee -a "$LOG_FILE" >/dev/null; }
log_ok()   { local m="$*"; echo "[$(date -Is)] OK    $m" | tee -a "$LOG_FILE" >/dev/null; _color "$C_GREEN"  "  [OK]    $m" >&2; }
log_warn() { local m="$*"; echo "[$(date -Is)] WARN  $m" | tee -a "$LOG_FILE" >/dev/null; _color "$C_YELLOW" "  [WARN]  $m" >&2; }
log_err()  { local m="$*"; echo "[$(date -Is)] ERROR $m" | tee -a "$LOG_FILE" >/dev/null; _color "$C_RED"    "  [ERROR] $m" >&2; }

# 让现场操作员一眼看到结果：横幅同时写入日志、stderr(journald)、物理控制台
broadcast() {
  local msg="$1"
  printf '%s\n' "$msg" | tee -a "$LOG_FILE" >&2
  if [ -w /dev/console ]; then
    printf '%s\n' "$msg" > /dev/console 2>/dev/null || true
  fi
}

broadcast_color() {
  local color="$1"; shift
  local plain="$*"
  _plain "$plain" | tee -a "$LOG_FILE" >/dev/null
  if _has_color; then
    printf "%s%s%s\n" "$color" "$plain" "$C_RESET" >&2
    if [ -w /dev/console ]; then
      printf "%s%s%s\n" "$color" "$plain" "$C_RESET" > /dev/console 2>/dev/null || true
    fi
  else
    printf '%s\n' "$plain" >&2
    if [ -w /dev/console ]; then
      printf '%s\n' "$plain" > /dev/console 2>/dev/null || true
    fi
  fi
}

banner_success() {
  local detail="${1:-}"
  broadcast ""
  broadcast_color "$C_GREEN"  "  ✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓"
  broadcast_color "$C_GREEN"  "  ✓                                                              ✓"
  broadcast_color "$C_GREEN"  "  ✓                   BOOTSTRAP  SUCCESS                         ✓"
  broadcast_color "$C_GREEN"  "  ✓                                                              ✓"
  [ -n "$detail" ] && broadcast_color "$C_GREEN" "  ✓  $detail"
  broadcast_color "$C_GREEN"  "  ✓  started:  ${BOOTSTRAP_STARTED_AT}"
  broadcast_color "$C_GREEN"  "  ✓  finished: $(date -Is)"
  broadcast_color "$C_GREEN"  "  ✓                                                              ✓"
  broadcast_color "$C_GREEN"  "  ✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓✓"
  broadcast ""

  # 大字绿色最终成功横幅
  if _has_color; then
    broadcast_color "$C_BG_GREEN"  "                                                              "
    broadcast_color "$C_BG_GREEN"  "     PROVISIONING  COMPLETE                                    "
    broadcast_color "$C_BG_GREEN"  "                                                              "
    broadcast_color "$C_BG_GREEN"  "     All edge services are running.                            "
    broadcast_color "$C_BG_GREEN"  "     Device: ${DEVICE_ID:-?}  Node: ${NODE_NAME:-?}            "
    broadcast_color "$C_BG_GREEN"  "                                                              "
  fi
  broadcast ""
}

banner_failure() {
  local exit_code="$1"
  local reason="${2:-(unspecified)}"
  broadcast ""
  broadcast_color "$C_RED"  "  ✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗"
  broadcast_color "$C_RED"  "  ✗                                                              ✗"
  broadcast_color "$C_RED"  "  ✗                   BOOTSTRAP  FAILED                           ✗"
  broadcast_color "$C_RED"  "  ✗                                                              ✗"
  broadcast_color "$C_RED"  "  ✗  exit_code: ${exit_code}"
  broadcast_color "$C_RED"  "  ✗  reason:    ${reason}"
  broadcast_color "$C_RED"  "  ✗  started:   ${BOOTSTRAP_STARTED_AT}"
  broadcast_color "$C_RED"  "  ✗  failed_at: $(date -Is)"
  broadcast_color "$C_RED"  "  ✗  log:       ${LOG_FILE}"
  broadcast_color "$C_RED"  "  ✗  next:      检查日志，修正后 reboot 或 systemctl restart"
  broadcast_color "$C_RED"  "  ✗                                                              ✗"
  broadcast_color "$C_RED"  "  ✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗✗"
  broadcast ""
}

write_status() {
  local state="$1"
  local exit_code="${2:-}"
  local reason="${3:-}"
  {
    echo "state=${state}"
    echo "started_at=${BOOTSTRAP_STARTED_AT}"
    echo "updated_at=$(date -Is)"
    [ -n "$exit_code" ] && echo "exit_code=${exit_code}"
    [ -n "$reason" ] && echo "reason=${reason}"
    [ -n "${DEVICE_ID:-}" ] && echo "device_id=${DEVICE_ID}"
    [ -n "${NODE_NAME:-}" ] && echo "node_name=${NODE_NAME}"
  } > "${STATUS_FILE}.tmp"
  mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
  chmod 644 "$STATUS_FILE" || true
}

fail() {
  BOOTSTRAP_FAIL_REASON="$1"
  exit "${2:-1}"
}

on_exit() {
  local rc=$?
  if [ "$rc" -eq 0 ]; then
    if [ -f "$SENTINEL" ]; then
      write_status "success" "0" ""
      banner_success "device_id: ${DEVICE_ID:-?}    node: ${NODE_NAME:-?}"
    fi
    # rc=0 但无 sentinel（例如 already-bootstrapped 早退）—— 不打横幅，不改 status
  else
    write_status "failed" "$rc" "${BOOTSTRAP_FAIL_REASON:-(see log)}"
    banner_failure "$rc" "${BOOTSTRAP_FAIL_REASON:-(see log)}"
  fi
  # 调用原有的 secrets 清理（合并到统一 trap 里，避免后定义的 trap 覆盖本 trap）
  if declare -F cleanup_secret_temp_files >/dev/null 2>&1; then
    cleanup_secret_temp_files || true
  fi
}
trap on_exit EXIT

write_status "running" "" ""

SECRET_TEMP_FILES=()
cleanup_secret_temp_files() {
  local f
  for f in "${SECRET_TEMP_FILES[@]:-}"; do
    if [ -n "${f:-}" ]; then
      rm -f "$f"
    fi
  done
}
# 注：原本这里有一个 `trap cleanup_secret_temp_files EXIT`，已合并到上方 `on_exit` 中。

secret_mktemp() {
  local f
  f="$(mktemp)"
  chmod 600 "$f"
  SECRET_TEMP_FILES+=("$f")
  echo "$f"
}

if [ -f "$SENTINEL" ]; then
  log "already bootstrapped: $SENTINEL"
  exit 0
fi

find_provision_dir() {
  # 常见桌面/服务器自动挂载路径（含 bootstrap 自己挂的 /mnt/drycontroller-provisioning）
  local roots=(/media /mnt /run/media)
  for r in "${roots[@]}"; do
    [ -d "$r" ] || continue
    # 遍历最多两级，避免扫太深
    while IFS= read -r -d '' p; do
      if [ -f "$p/provisioning.json" ]; then
        echo "$p"
        return 0
      fi
    done < <(find "$r" -maxdepth 3 -type d -name "drycontroller-provisioning" -print0 2>/dev/null || true)
  done
  return 1
}

# Server 版 Ubuntu 不会自动挂 USB；这里主动扫块设备并挂载。
# 探测策略：找所有 vfat/ext* 分区，临时只读挂上看里面有没有 drycontroller-provisioning/provisioning.json。
# 找到后挂到 $BOOTSTRAP_USB_MOUNT (默认 /mnt/drycontroller-provisioning)。
mount_provisioning_usb() {
  local target="$BOOTSTRAP_USB_MOUNT"
  mkdir -p "$target"

  # 已经挂着且内容齐全 —— 直接复用
  if mountpoint -q "$target" && [ -f "$target/drycontroller-provisioning/provisioning.json" ]; then
    log "provisioning USB already mounted at $target"
    return 0
  fi

  local timeout="${BOOTSTRAP_USB_WAIT_SEC:-60}"
  local interval=3
  local elapsed=0

  while [ "$elapsed" -lt "$timeout" ]; do
    udevadm settle --timeout=5 >/dev/null 2>&1 || true

    local candidates
    candidates="$(lsblk -lnpo NAME,TYPE,FSTYPE 2>/dev/null \
      | awk '$2=="part" && ($3=="vfat" || $3 ~ /^ext/) {print $1}')"

    local dev
    for dev in $candidates; do
      [ -b "$dev" ] || continue
      local probe
      probe="$(mktemp -d)"
      if mount -o ro "$dev" "$probe" >/dev/null 2>&1; then
        if [ -f "$probe/drycontroller-provisioning/provisioning.json" ]; then
          umount "$probe" >/dev/null 2>&1 || true
          rmdir "$probe" 2>/dev/null || true
          if mount -o ro "$dev" "$target" >/dev/null 2>&1; then
            log "mounted provisioning USB: $dev -> $target"
            return 0
          else
            log "found provisioning data on $dev but failed to mount at $target"
          fi
        else
          umount "$probe" >/dev/null 2>&1 || true
        fi
      fi
      rmdir "$probe" 2>/dev/null || true
    done

    log_warn "provisioning USB not yet visible (elapsed ${elapsed}s/${timeout}s)"
    sleep "$interval"
    elapsed=$((elapsed + interval))
  done
  return 1
}

PROV_DIR="$(find_provision_dir || true)"
if [ -z "${PROV_DIR:-}" ]; then
  log "no pre-mounted provisioning dir; attempting to mount USB ourselves..."
  if mount_provisioning_usb; then
    PROV_DIR="$(find_provision_dir || true)"
  fi
fi
if [ -z "${PROV_DIR:-}" ]; then
  log "provisioning package not found. Please insert USB with drycontroller-provisioning/ and reboot."
  fail "provisioning USB not found (scanned /media /mnt /run/media + all vfat/ext partitions for ${BOOTSTRAP_USB_WAIT_SEC:-60}s)" 2
fi

log_ok "found provisioning dir: $PROV_DIR"

PROV_JSON="$PROV_DIR/provisioning.json"
COSIGN_PUB="$PROV_DIR/cosign.pub"

if [ ! -f "$PROV_JSON" ]; then
  log "missing provisioning.json: $PROV_JSON"
  exit 2
fi
if [ ! -f "$COSIGN_PUB" ]; then
  # fallback：U 盘未提供 cosign.pub 时使用仓库内置版本（dev / 应急装机用）
  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  REPO_PUB="${REPO_ROOT}/configs/keys/cosign.pub"
  if [ -f "$REPO_PUB" ]; then
    log_warn "U 盘未提供 cosign.pub，回退使用仓库内置: $REPO_PUB"
    COSIGN_PUB="$REPO_PUB"
  else
    log "missing cosign.pub: $COSIGN_PUB (and no fallback at $REPO_PUB)"
    exit 2
  fi
fi

# 简单 JSON 解析（避免依赖 jq）：使用 python3
pyget() {
  local key="$1"
  python3 - <<PY
import json,sys
obj=json.load(open("${PROV_JSON}","r",encoding="utf-8"))
v=obj.get("${key}")
if v is None:
  sys.exit(1)
if isinstance(v,(dict,list)):
  print(json.dumps(v,ensure_ascii=False))
else:
  print(str(v))
PY
}

DEVICE_ID="$(pyget device_id || true)"
TENANT_ID="$(pyget tenant_id || true)"
LINE_ID="$(pyget line_id || true)"
NODE_NAME="$(pyget node_name || true)"
MACHINE_ID="$(cat /etc/machine-id 2>/dev/null || true)"

CLOUD_OPS_BASE_URL="$(pyget cloud_ops_base_url || true)"
CLOUD_OPS_TAILNET_BASE_URL="$(pyget cloud_ops_tailnet_base_url || true)"
ENROLL_TOKEN="$(pyget enroll_token || true)"
BATCH_ID="$(pyget batch_id || true)"
INSTALLER_TOKEN="$(pyget installer_token || true)"

TS_LOGIN_SERVER="$(pyget ts_login_server || true)"
TS_AUTHKEY="$(pyget ts_authkey || true)"
TS_HOSTNAME="$(pyget ts_hostname || true)"
TS_HOSTNAME="${TS_HOSTNAME:-${NODE_NAME:-$(hostname)}}"

HARBOR_HOST="$(pyget harbor_host || true)"
HARBOR_ROBOT_USER="$(pyget harbor_robot_user || true)"
HARBOR_ROBOT_TOKEN="$(pyget harbor_robot_token || true)"
TAILSCALE_IMAGE="$(pyget tailscale_image || true)"
TAILSCALE_IMAGE="${TAILSCALE_IMAGE:-git.example.com/example-org/tailscale:stable}"

if [ -z "${CLOUD_OPS_BASE_URL:-}" ]; then
  log "missing cloud_ops_base_url in provisioning.json"
  exit 2
fi

EDGE_ROOT="/opt/Drycontroller-edge"
if [ -d "/opt/drycontroller-edge" ]; then
  EDGE_ROOT="/opt/drycontroller-edge"
fi
if [ -d "/opt/Drycontroller-edge" ]; then
  EDGE_ROOT="/opt/Drycontroller-edge"
fi
if [ ! -f "${EDGE_ROOT}/docker-compose.yml" ]; then
  # 允许从当前目录执行（开发/调试）
  if [ -f "/work/docker-compose.yml" ]; then
    EDGE_ROOT="/work"
  fi
fi
if [ ! -f "${EDGE_ROOT}/docker-compose.yml" ]; then
  log "edge repo not found at /opt/*; expected docker-compose.yml. Please install Drycontroller-edge to /opt."
  exit 4
fi

ENV_FILE="${EDGE_ROOT}/.env"
if [ ! -f "$ENV_FILE" ]; then
  if [ -f "${EDGE_ROOT}/env.example" ]; then
    cp "${EDGE_ROOT}/env.example" "$ENV_FILE"
    log "created .env from env.example"
  else
    touch "$ENV_FILE"
    log "created empty .env"
  fi
fi

set_kv() {
  local k="$1"
  local v="$2"
  ENV_FILE="$ENV_FILE" ENV_KEY="$k" ENV_VALUE="$v" python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["ENV_FILE"])
key = os.environ["ENV_KEY"]
value = os.environ["ENV_VALUE"]
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
replacement = f"{key}={value}"
out = []
replaced = False
for line in lines:
    if line.startswith(f"{key}="):
        if not replaced:
            out.append(replacement)
            replaced = True
        continue
    out.append(line)
if not replaced:
    out.append(replacement)
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
tmp.replace(path)
PY
}

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  else
    log "docker compose v2 plugin is required"
    exit 4
  fi
}

REGISTRY_LOGIN_DONE=0
registry_login() {
  if [ "$REGISTRY_LOGIN_DONE" = "1" ]; then
    return 0
  fi
  if [ -z "${HARBOR_HOST:-}" ] || [ -z "${HARBOR_ROBOT_USER:-}" ] || [ -z "${HARBOR_ROBOT_TOKEN:-}" ]; then
    return 1
  fi
  log "docker login to ${HARBOR_HOST} ..."
  if echo "${HARBOR_ROBOT_TOKEN}" | docker login "${HARBOR_HOST}" -u "${HARBOR_ROBOT_USER}" --password-stdin >> "$LOG_FILE" 2>&1; then
    REGISTRY_LOGIN_DONE=1
    log_ok "docker login ok — ${HARBOR_HOST}"
    return 0
  fi
  log_err "docker login failed — ${HARBOR_HOST}"
  return 1
}

load_bootstrap_image_tars() {
  local images_dir="${PROV_DIR}/images"
  [ -d "$images_dir" ] || return 0

  local found=0
  while IFS= read -r -d '' image_tar; do
    found=1
    log "loading bootstrap image from USB: ${image_tar}"
    docker load -i "$image_tar" >> "$LOG_FILE" 2>&1 || {
      log "docker load failed: ${image_tar}"
      return 1
    }
  done < <(find "$images_dir" -maxdepth 1 -type f \( -name '*.tar' -o -name '*.tar.gz' -o -name '*.tgz' \) -print0 2>/dev/null | sort -z)

  if [ "$found" = "0" ]; then
    log "bootstrap images directory exists but contains no image tar: ${images_dir}"
  fi
  return 0
}

ensure_docker_image() {
  local local_image="$1"
  local mirror_image="${2:-}"

  if docker image inspect "$local_image" >/dev/null 2>&1; then
    log "required image already local: ${local_image}"
    return 0
  fi

  # USB / bundle 内预构建镜像优先——零网络，本地秒级加载
  load_bootstrap_image_tars
  if docker image inspect "$local_image" >/dev/null 2>&1; then
    log "required image loaded from USB/bundle: ${local_image}"
    return 0
  fi

  # 网络回退默认关闭。现场有网且 USB 缺镜像时，设置 BOOTSTRAP_ALLOW_NETWORK_FALLBACK=true
  if [ "${BOOTSTRAP_ALLOW_NETWORK_FALLBACK:-false}" != "true" ]; then
    log "required image unavailable (no USB/bundle match): ${local_image}"
    log "to allow network pull, set BOOTSTRAP_ALLOW_NETWORK_FALLBACK=true in provisioning environment"
    return 1
  fi

  log "network fallback enabled; attempting online pull for ${local_image}"

  if [ -n "$mirror_image" ]; then
    if registry_login; then
      log "pulling required image from private registry: ${mirror_image}"
      if docker pull "$mirror_image" >> "$LOG_FILE" 2>&1; then
        if [ "$mirror_image" != "$local_image" ]; then
          docker tag "$mirror_image" "$local_image"
        fi
        log "required image available from private registry: ${local_image}"
        return 0
      fi
      log "private registry pull failed: ${mirror_image}"
    else
      log "private registry credentials unavailable before image pull"
    fi
  fi

  log "pulling required image from Docker Hub: ${local_image}"
  if docker pull "$local_image" >> "$LOG_FILE" 2>&1; then
    log "required image pulled from Docker Hub: ${local_image}"
    return 0
  fi
  log "Docker Hub pull failed: ${local_image}"

  log "required image unavailable after all fallbacks: ${local_image}"
  return 1
}

CLOUD_OPS_ALLOCATE_BASE_URL="$CLOUD_OPS_BASE_URL"
if [ -n "${CLOUD_OPS_TAILNET_BASE_URL:-}" ]; then
  if [ -z "${TS_LOGIN_SERVER:-}" ] || [ -z "${TS_AUTHKEY:-}" ]; then
    log "cloud_ops_tailnet_base_url requires ts_login_server and ts_authkey in provisioning.json before allocation"
    exit 2
  fi
  if ! command -v docker >/dev/null 2>&1; then
    log "docker not found"
    exit 3
  fi
  set_kv "TS_LOGIN_SERVER" "${TS_LOGIN_SERVER}"
  set_kv "TS_AUTHKEY" "${TS_AUTHKEY}"
  set_kv "TS_HOSTNAME" "${TS_HOSTNAME}"
  set_kv "TS_EXTRA_ARGS" "--login-server=${TS_LOGIN_SERVER}"
  # P1-N15 path B: install Headscale self-signed CA before Tailscale starts,
  # so the client trusts the coordination server.  When ICP filing completes
  # (path A), omit headscale_ca from provisioning.json and this block is a no-op.
  HEADSCALE_CA="$(pyget headscale_ca 2>/dev/null || true)"
  if [ -n "${HEADSCALE_CA:-}" ]; then
    HEADSCALE_CA_CRT="/etc/drycontroller/keys/headscale-ca.crt"
    mkdir -p /etc/drycontroller/keys
    printf '%s' "${HEADSCALE_CA}" | base64 -d > "${HEADSCALE_CA_CRT}"
    chmod 644 "${HEADSCALE_CA_CRT}"
    if [ -d /usr/local/share/ca-certificates ]; then
      cp "${HEADSCALE_CA_CRT}" /usr/local/share/ca-certificates/headscale-ca.crt
      update-ca-certificates 2>/dev/null || true
      log_ok "headscale CA installed to system trust store"
    else
      log "headscale CA cert written to ${HEADSCALE_CA_CRT} (no system ca-certificates dir)"
    fi
  fi
  cd "$EDGE_ROOT"
  ensure_docker_image "tailscale/tailscale:stable" "${TAILSCALE_IMAGE}" || exit 3
  log "starting tailnet before cloud allocation..."
  compose --profile remote-debug up -d tailscale
  TAILNET_READY=0
  TAILNET_RETRIES="${BOOTSTRAP_TAILNET_RETRIES:-60}"
  TAILNET_SLEEP_SEC="${BOOTSTRAP_TAILNET_SLEEP_SEC:-2}"
  for attempt in $(seq 1 "$TAILNET_RETRIES"); do
    if docker exec edge-tailscale tailscale ip -4 >/dev/null 2>&1 \
       && docker exec edge-tailscale wget -q -T 5 -O /dev/null "${CLOUD_OPS_TAILNET_BASE_URL%/}/health" >/dev/null 2>&1; then
      TAILNET_READY=1
      break
    fi
    log "tailnet not ready before allocation (attempt ${attempt}/${TAILNET_RETRIES})"
    sleep "$TAILNET_SLEEP_SEC"
  done
  if [ "$TAILNET_READY" = "1" ]; then
    CLOUD_OPS_ALLOCATE_BASE_URL="$CLOUD_OPS_TAILNET_BASE_URL"
    log_ok "tailnet ready before allocation; allocating through ${CLOUD_OPS_ALLOCATE_BASE_URL}"
  else
    log_err "tailnet did not become ready before allocation; bootstrap will not fall back to public cloud url"
    exit 5
  fi
fi

ALLOCATION_RESP_FILE=""
if [ -n "${BATCH_ID:-}" ] || [ -n "${INSTALLER_TOKEN:-}" ]; then
  if [ -z "${BATCH_ID:-}" ] || [ -z "${INSTALLER_TOKEN:-}" ]; then
    log "batch_id and installer_token must be provided together"
    exit 2
  fi

  ALLOCATE_PAYLOAD="$(PROV_JSON="$PROV_JSON" MACHINE_ID="$MACHINE_ID" python3 - <<'PY'
import json
import os

obj = json.load(open(os.environ["PROV_JSON"], "r", encoding="utf-8"))
payload = {
  "batch_id": obj.get("batch_id", ""),
  "installer_token": obj.get("installer_token", ""),
  "machine_id": os.environ.get("MACHINE_ID", ""),
}
for key in (
  "tenant_id",
  "line_id",
  "ts_hostname",
  "ts_login_server",
  "ts_authkey",
  "harbor_host",
  "harbor_robot_user",
  "harbor_robot_token",
  "meta",
):
  value = obj.get(key)
  if value:
    payload[key] = value
print(json.dumps(payload, ensure_ascii=False))
PY
)"
  ALLOCATION_RESP_FILE="$(secret_mktemp)"
  if [ -f "$ALLOCATION_CACHE_FILE" ]; then
    if BATCH_ID="$BATCH_ID" python3 - "$ALLOCATION_CACHE_FILE" <<'PY'
import json
import os
import sys

try:
    resp = json.load(open(sys.argv[1], "r", encoding="utf-8"))
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if resp.get("batch_id") == os.environ.get("BATCH_ID") else 1)
PY
    then
      log "using cached cloud allocation response: $ALLOCATION_CACHE_FILE"
      cp "$ALLOCATION_CACHE_FILE" "$ALLOCATION_RESP_FILE"
    else
      log "cached allocation response does not match current batch; ignoring cache"
      rm -f "$ALLOCATION_CACHE_FILE"
    fi
  fi
  if [ ! -s "$ALLOCATION_RESP_FILE" ]; then
    log "allocating device identity from cloud-ops batch ${BATCH_ID}..."
    if curl -fsS "${CLOUD_OPS_ALLOCATE_BASE_URL%/}/api/v1/provisioning/allocate" \
      -H "Content-Type: application/json" \
      -d "$ALLOCATE_PAYLOAD" \
      -o "$ALLOCATION_RESP_FILE" >> "$LOG_FILE" 2>&1; then
      install -m 0600 "$ALLOCATION_RESP_FILE" "$ALLOCATION_CACHE_FILE"
      log_ok "cloud allocation ok; cached response -> $ALLOCATION_CACHE_FILE"
    else
      log_err "cloud allocation failed; bootstrap will not be marked complete"
      exit 5
    fi
  fi

  eval "$(python3 - "$ALLOCATION_RESP_FILE" <<'PY'
import json
import shlex
import sys

resp = json.load(open(sys.argv[1], "r", encoding="utf-8"))
mapping = {
  "DEVICE_ID": resp.get("device_id"),
  "TENANT_ID": resp.get("tenant_id"),
  "LINE_ID": resp.get("line_id"),
  "NODE_NAME": resp.get("node_name"),
  "TS_HOSTNAME": resp.get("ts_hostname") or resp.get("node_name"),
  "ENROLL_TOKEN": resp.get("enroll_token"),
  "TS_LOGIN_SERVER": resp.get("ts_login_server"),
  "TS_AUTHKEY": resp.get("ts_authkey"),
  "HARBOR_HOST": resp.get("harbor_host"),
  "HARBOR_ROBOT_USER": resp.get("harbor_robot_user"),
  "HARBOR_ROBOT_TOKEN": resp.get("harbor_robot_token"),
  "ADMIN_API_KEYS": resp.get("admin_api_key"),
  "CONTROL_API_KEYS": resp.get("control_api_key"),
  "NODE_ENROLL_TOKEN": resp.get("node_enroll_token"),
}
for key, value in mapping.items():
  if value is not None:
    print(f"{key}={shlex.quote(str(value))}")
PY
)"
fi

if [ -z "${ENROLL_TOKEN:-}" ]; then
  log "missing enroll_token in provisioning.json or cloud allocation response"
  exit 2
fi
NODE_ENROLL_TOKEN="${NODE_ENROLL_TOKEN:-$ENROLL_TOKEN}"

if [ -z "${DEVICE_ID:-}" ] || [ -z "${NODE_NAME:-}" ]; then
  log "missing device_id or node_name in provisioning.json or cloud allocation response"
  exit 2
fi

if [ -z "${TS_LOGIN_SERVER:-}" ] || [ -z "${TS_AUTHKEY:-}" ]; then
  log "missing ts_login_server or ts_authkey in provisioning.json or cloud allocation response"
  exit 2
fi

if [ -z "${HARBOR_HOST:-}" ] || [ -z "${HARBOR_ROBOT_USER:-}" ] || [ -z "${HARBOR_ROBOT_TOKEN:-}" ]; then
  log "missing harbor credentials in provisioning.json or cloud allocation response"
  exit 2
fi

CLOUD_OPS_RUNTIME_BASE_URL="${CLOUD_OPS_TAILNET_BASE_URL:-$CLOUD_OPS_BASE_URL}"

# 1) 写入运维资产：cosign.pub
install -m 0644 "$COSIGN_PUB" "${KEYS_DIR}/cosign.pub"
log "installed cosign pubkey -> ${KEYS_DIR}/cosign.pub"

# 2) 基础命令检查 + docker login（Harbor 是公网入口，与 tailnet 无关，前置以早 fail）
if ! command -v docker >/dev/null 2>&1; then
  log "docker not found"
  exit 3
fi

registry_login || exit 3

set_kv "TS_LOGIN_SERVER" "${TS_LOGIN_SERVER}"
set_kv "TS_AUTHKEY" "${TS_AUTHKEY}"
set_kv "TS_HOSTNAME" "${TS_HOSTNAME}"
set_kv "TS_EXTRA_ARGS" "--login-server=${TS_LOGIN_SERVER}"
# 云分配模式下，edge-api 只通过 tailnet 对云端开放探活/运维入口。
# 默认 compose 仍保持 127.0.0.1，避免未入网机器在现场 LAN 暴露管理口。
set_kv "EDGE_API_BIND_ADDR" "127.0.0.1"
set_kv "CLOUD_OPS_BASE_URL" "${CLOUD_OPS_RUNTIME_BASE_URL}"
set_kv "CLOUD_OPS_ENROLL_TOKEN" "${NODE_ENROLL_TOKEN}"
set_kv "LICENSE_FILE_PATH" "/etc/drycontroller/license/license.json"
set_kv "LICENSE_PUBLIC_KEY_PATH" "/etc/drycontroller/keys/license.pub"
if [ -n "${DEVICE_ID:-}" ]; then set_kv "DEVICE_ID" "${DEVICE_ID}"; fi
set_json_list_kv() {
  local k="$1"
  local v="$2"
  local encoded
  encoded="$(ENV_VALUE="$v" python3 - <<'PY'
import json
import os

value = os.environ["ENV_VALUE"]
print(json.dumps([value], ensure_ascii=False))
PY
)"
  set_kv "$k" "$encoded"
}

if [ -n "${ADMIN_API_KEYS:-}" ]; then set_json_list_kv "ADMIN_API_KEYS" "${ADMIN_API_KEYS}"; fi
if [ -n "${CONTROL_API_KEYS:-}" ]; then set_json_list_kv "CONTROL_API_KEYS" "${CONTROL_API_KEYS}"; fi
if [ -n "${MACHINE_ID:-}" ]; then set_kv "LICENSE_MACHINE_ID" "${MACHINE_ID}"; fi
if [ -n "${LINE_ID:-}" ]; then set_kv "LINE_ID" "${LINE_ID}"; fi
set_kv "CLOUD_OPS_PUBLIC_BASE_URL" "${CLOUD_OPS_BASE_URL}"
if [ -n "${CLOUD_OPS_TAILNET_BASE_URL:-}" ]; then
  set_kv "CLOUD_OPS_TAILNET_BASE_URL" "${CLOUD_OPS_TAILNET_BASE_URL}"
fi
log "updated edge .env at ${ENV_FILE}"

# 4) 启动 tailnet。Headscale-first：后续 enroll/license/OTA 使用 tailnet Cloud Ops URL。
cd "$EDGE_ROOT"

ensure_docker_image "tailscale/tailscale:stable" "${TAILSCALE_IMAGE}" || exit 3
log "starting tailnet first (tailscale only)..."
compose --profile remote-debug up -d tailscale

if [ -n "${CLOUD_OPS_TAILNET_BASE_URL:-}" ]; then
  log "waiting for tailnet identity before cloud runtime calls..."
  TAILNET_READY=0
  TAILNET_IP=""
  TAILNET_RETRIES="${BOOTSTRAP_TAILNET_RETRIES:-60}"
  TAILNET_SLEEP_SEC="${BOOTSTRAP_TAILNET_SLEEP_SEC:-2}"
  for attempt in $(seq 1 "$TAILNET_RETRIES"); do
    # tailscale ip 证明拿到 tailnet IP；wget /health 证明 cloud-ops 经 tailnet 真可达（路由收敛 + ECS 端口放行）
    TAILNET_IP="$(docker exec edge-tailscale tailscale ip -4 2>/dev/null | head -n 1 || true)"
    if [ -n "$TAILNET_IP" ] \
       && docker exec edge-tailscale wget -q -T 5 -O /dev/null "${CLOUD_OPS_RUNTIME_BASE_URL%/}/health" >/dev/null 2>&1; then
      TAILNET_READY=1
      break
    fi
    log "tailnet not ready yet (attempt ${attempt}/${TAILNET_RETRIES})"
    sleep "$TAILNET_SLEEP_SEC"
  done
  if [ "$TAILNET_READY" = "1" ]; then
    log_ok "tailnet ready; cloud runtime url: ${CLOUD_OPS_RUNTIME_BASE_URL}"
    set_kv "EDGE_API_BIND_ADDR" "$TAILNET_IP"
    log "edge-api bind address set to tailnet ip: ${TAILNET_IP}"
  else
    log_err "tailnet did not become ready; bootstrap will not fall back to public cloud url"
    exit 5
  fi
else
  log "cloud_ops_tailnet_base_url not provided; using cloud_ops_base_url for runtime cloud calls"
fi

# 5) 启动 edge（含 enroll；以及 ota profile：edge-ota-agent）
log "starting edge core stack..."
compose up -d

log "starting tailnet enroll (profile remote-debug)..."
compose --profile remote-debug up -d edge-enroll

log "starting ota agent (profile ota)..."
compose --profile ota up -d edge-ota-agent

# 6) 云端登记/绑定 license，并保存本地签名 license
if [ -n "${ALLOCATION_RESP_FILE:-}" ] && [ -z "${CLOUD_OPS_TAILNET_BASE_URL:-}" ]; then
  LICENSE_RESP_FILE="$ALLOCATION_RESP_FILE"
  log "using signed license from cloud allocation response"
else
  PAYLOAD="$(DEVICE_ID="$DEVICE_ID" MACHINE_ID="$MACHINE_ID" TENANT_ID="$TENANT_ID" LINE_ID="$LINE_ID" NODE_NAME="$NODE_NAME" TS_HOSTNAME="$TS_HOSTNAME" python3 - <<'PY'
import json
import os

print(json.dumps({
  "device_id": os.environ.get("DEVICE_ID", ""),
  "machine_id": os.environ.get("MACHINE_ID", ""),
  "tenant_id": os.environ.get("TENANT_ID", ""),
  "line_id": os.environ.get("LINE_ID", ""),
  "node_name": os.environ.get("NODE_NAME", ""),
  "ts_hostname": os.environ.get("TS_HOSTNAME", ""),
}, ensure_ascii=False))
PY
)"
  log "binding license on cloud-ops..."
  LICENSE_RESP_FILE="$(secret_mktemp)"
  if curl -fsS "${CLOUD_OPS_RUNTIME_BASE_URL%/}/api/v1/provisioning/license/bind" \
    -H "Content-Type: application/json" \
    -H "X-Enroll-Token: ${ENROLL_TOKEN}" \
    -d "$PAYLOAD" \
    -o "$LICENSE_RESP_FILE" >> "$LOG_FILE" 2>&1; then
    log_ok "license bind request ok"
  else
    log_err "license bind request failed; bootstrap will not be marked complete"
    exit 5
  fi
fi

python3 - "$LICENSE_RESP_FILE" "${LICENSE_DIR}/license.json" "${KEYS_DIR}/license.pub" <<'PY'
import json
import sys
from pathlib import Path

resp = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
license_payload = resp.get("license") or resp.get("payload")
public_key = (resp.get("license_public_key") or "").strip()
if not isinstance(license_payload, dict) or not license_payload.get("signature"):
    raise SystemExit("license response missing signed license")
if not public_key:
    raise SystemExit("license response missing license_public_key")
Path(sys.argv[2]).write_text(json.dumps(license_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
Path(sys.argv[3]).write_text(public_key + "\n", encoding="utf-8")
PY
rm -f "$LICENSE_RESP_FILE"
chmod 0640 "${LICENSE_DIR}/license.json" || true
chmod 0640 "${KEYS_DIR}/license.pub" || true
log "saved signed license -> ${LICENSE_DIR}/license.json"
log "saved license public key -> ${KEYS_DIR}/license.pub"

log "verifying local license..."
VERIFY_URL="http://127.0.0.1:${LICENSE_SERVICE_PORT:-8004}/verify"
VERIFY_RETRIES="${BOOTSTRAP_LICENSE_VERIFY_RETRIES:-10}"
VERIFY_SLEEP_SEC="${BOOTSTRAP_LICENSE_VERIFY_SLEEP_SEC:-2}"
VERIFY_OK=0
for attempt in $(seq 1 "$VERIFY_RETRIES"); do
  if curl -fsS "$VERIFY_URL" | python3 -c 'import json,sys; data=json.load(sys.stdin); assert data.get("valid") is True, data'; then
    VERIFY_OK=1
    break
  fi
  log "local license verify not ready or invalid (attempt ${attempt}/${VERIFY_RETRIES})"
  sleep "$VERIFY_SLEEP_SEC"
done
if [ "$VERIFY_OK" = "1" ]; then
  log_ok "local license verify ok"
else
  log_err "local license verify failed; bootstrap will not be marked complete"
  exit 6
fi

DEVICE_JSON="${STATE_DIR}/device.json" \
DEVICE_ID="$DEVICE_ID" \
TENANT_ID="$TENANT_ID" \
LINE_ID="$LINE_ID" \
NODE_NAME="$NODE_NAME" \
TS_HOSTNAME="$TS_HOSTNAME" \
CLOUD_OPS_BASE_URL="$CLOUD_OPS_BASE_URL" \
CLOUD_OPS_RUNTIME_BASE_URL="$CLOUD_OPS_RUNTIME_BASE_URL" \
CLOUD_OPS_TAILNET_BASE_URL="$CLOUD_OPS_TAILNET_BASE_URL" \
ACTIVATED_AT="$(date -Is)" \
BOOTSTRAPPED_AT="$(date -Is)" \
python3 - <<'PY'
import json
import os
from pathlib import Path

payload = {
    "device_id": os.environ.get("DEVICE_ID", ""),
    "tenant_id": os.environ.get("TENANT_ID", ""),
    "line_id": os.environ.get("LINE_ID", ""),
    "node_name": os.environ.get("NODE_NAME", ""),
    "ts_hostname": os.environ.get("TS_HOSTNAME", ""),
    "cloud_ops_base_url": os.environ.get("CLOUD_OPS_RUNTIME_BASE_URL", ""),
    "cloud_ops_public_base_url": os.environ.get("CLOUD_OPS_BASE_URL", ""),
    "cloud_ops_tailnet_base_url": os.environ.get("CLOUD_OPS_TAILNET_BASE_URL", ""),
    "activated_at": os.environ.get("ACTIVATED_AT", ""),
    "bootstrapped_at": os.environ.get("BOOTSTRAPPED_AT", ""),
}
Path(os.environ["DEVICE_JSON"]).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

touch "$SENTINEL"
log_ok "bootstrap complete. sentinel written: $SENTINEL"
