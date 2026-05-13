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
ASSET_DIR="/etc/drycontroller"
KEYS_DIR="${ASSET_DIR}/keys"

mkdir -p "$(dirname "$LOG_FILE")" "$STATE_DIR" "$KEYS_DIR"
chmod 700 "$STATE_DIR" || true

log() { echo "[$(date -Is)] $*" | tee -a "$LOG_FILE" >/dev/null; }

if [ -f "$SENTINEL" ]; then
  log "already bootstrapped: $SENTINEL"
  exit 0
fi

find_provision_dir() {
  # 常见桌面/服务器自动挂载路径
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

PROV_DIR="$(find_provision_dir || true)"
if [ -z "${PROV_DIR:-}" ]; then
  log "provisioning package not found. Please insert USB with drycontroller-provisioning/ and reboot."
  exit 0
fi

log "found provisioning dir: $PROV_DIR"

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
    log "U 盘未提供 cosign.pub，回退使用仓库内置: $REPO_PUB"
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

CLOUD_OPS_BASE_URL="$(pyget cloud_ops_base_url || true)"
ENROLL_TOKEN="$(pyget enroll_token || true)"

TS_LOGIN_SERVER="$(pyget ts_login_server || true)"
TS_AUTHKEY="$(pyget ts_authkey || true)"
TS_HOSTNAME="${NODE_NAME:-$(hostname)}"

HARBOR_HOST="$(pyget harbor_host || true)"
HARBOR_ROBOT_USER="$(pyget harbor_robot_user || true)"
HARBOR_ROBOT_TOKEN="$(pyget harbor_robot_token || true)"

if [ -z "${CLOUD_OPS_BASE_URL:-}" ] || [ -z "${ENROLL_TOKEN:-}" ]; then
  log "missing cloud_ops_base_url or enroll_token in provisioning.json"
  exit 2
fi

if [ -z "${TS_LOGIN_SERVER:-}" ] || [ -z "${TS_AUTHKEY:-}" ]; then
  log "missing ts_login_server or ts_authkey in provisioning.json"
  exit 2
fi

if [ -z "${HARBOR_HOST:-}" ] || [ -z "${HARBOR_ROBOT_USER:-}" ] || [ -z "${HARBOR_ROBOT_TOKEN:-}" ]; then
  log "missing harbor credentials in provisioning.json"
  exit 2
fi

# 1) 写入运维资产：cosign.pub
install -m 0644 "$COSIGN_PUB" "${KEYS_DIR}/cosign.pub"
log "installed cosign pubkey -> ${KEYS_DIR}/cosign.pub"

# 2) docker login（只读 robot）
if command -v docker >/dev/null 2>&1; then
  log "docker login to ${HARBOR_HOST} ..."
  echo "${HARBOR_ROBOT_TOKEN}" | docker login "${HARBOR_HOST}" -u "${HARBOR_ROBOT_USER}" --password-stdin \
    && log "docker login ok" \
    || { log "docker login failed"; exit 3; }
else
  log "docker not found"
  exit 3
fi

# 3) 写入 drycontroller-edge .env（若不存在则生成；若存在则只补齐关键字段）
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
  # 若已有 key 则替换，否则追加
  if grep -qE "^${k}=" "$ENV_FILE"; then
    sed -i "s#^${k}=.*#${k}=${v}#g" "$ENV_FILE"
  else
    echo "${k}=${v}" >> "$ENV_FILE"
  fi
}

set_kv "TS_LOGIN_SERVER" "${TS_LOGIN_SERVER}"
set_kv "TS_AUTHKEY" "${TS_AUTHKEY}"
set_kv "TS_HOSTNAME" "${TS_HOSTNAME}"
set_kv "CLOUD_OPS_BASE_URL" "${CLOUD_OPS_BASE_URL}"
set_kv "CLOUD_OPS_ENROLL_TOKEN" "${ENROLL_TOKEN}"
if [ -n "${LINE_ID:-}" ]; then set_kv "LINE_ID" "${LINE_ID}"; fi
log "updated edge .env at ${ENV_FILE}"

# 4) 启动 edge（含 remote-debug：tailscale+edge-enroll；以及 ota profile：edge-ota-agent）
cd "$EDGE_ROOT"

compose() {
  if command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    docker compose "$@"
  fi
}

log "starting edge core stack..."
compose up -d

log "starting tailnet/enroll (profile remote-debug)..."
compose --profile remote-debug up -d tailscale edge-enroll

log "starting ota agent (profile ota)..."
compose --profile ota up -d edge-ota-agent

# 5) 云端登记/绑定 license（需要 cloud-ops 新接口）
MACHINE_ID="$(cat /etc/machine-id 2>/dev/null || true)"
PAYLOAD="$(python3 - <<PY
import json
print(json.dumps({
  "device_id": "${DEVICE_ID}",
  "machine_id": "${MACHINE_ID}",
  "tenant_id": "${TENANT_ID}",
  "line_id": "${LINE_ID}",
  "node_name": "${NODE_NAME}",
  "ts_hostname": "${TS_HOSTNAME}",
}, ensure_ascii=False))
PY
)"
log "binding license on cloud-ops..."
curl -fsS "${CLOUD_OPS_BASE_URL%/}/api/v1/provisioning/license/bind" \
  -H "Content-Type: application/json" \
  -H "X-Enroll-Token: ${ENROLL_TOKEN}" \
  -d "$PAYLOAD" \
  >> "$LOG_FILE" 2>&1 \
  && log "license bind request ok" \
  || log "WARN: license bind request failed (see log)"

cat > "${STATE_DIR}/device.json" <<EOF
{
  "device_id": "${DEVICE_ID}",
  "tenant_id": "${TENANT_ID}",
  "line_id": "${LINE_ID}",
  "node_name": "${NODE_NAME}",
  "ts_hostname": "${TS_HOSTNAME}",
  "cloud_ops_base_url": "${CLOUD_OPS_BASE_URL}",
  "bootstrapped_at": "$(date -Is)"
}
EOF

touch "$SENTINEL"
log "bootstrap complete. sentinel written: $SENTINEL"


