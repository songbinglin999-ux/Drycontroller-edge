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
ALLOCATION_CACHE_FILE="${STATE_DIR}/allocation-response.json"
ASSET_DIR="/etc/drycontroller"
KEYS_DIR="${ASSET_DIR}/keys"
LICENSE_DIR="${ASSET_DIR}/license"

mkdir -p "$(dirname "$LOG_FILE")" "$STATE_DIR" "$KEYS_DIR" "$LICENSE_DIR"
chmod 700 "$STATE_DIR" || true
chmod 755 "$ASSET_DIR" "$KEYS_DIR" || true
chmod 750 "$LICENSE_DIR" || true

log() { echo "[$(date -Is)] $*" | tee -a "$LOG_FILE" >/dev/null; }

SECRET_TEMP_FILES=()
cleanup_secret_temp_files() {
  local f
  for f in "${SECRET_TEMP_FILES[@]:-}"; do
    if [ -n "${f:-}" ]; then
      rm -f "$f"
    fi
  done
}
trap cleanup_secret_temp_files EXIT

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
  cd "$EDGE_ROOT"
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
    log "tailnet ready before allocation; allocating through ${CLOUD_OPS_ALLOCATE_BASE_URL}"
  else
    log "tailnet did not become ready before allocation; bootstrap will not fall back to public cloud url"
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
      log "cloud allocation ok; cached response -> $ALLOCATION_CACHE_FILE"
    else
      log "cloud allocation failed; bootstrap will not be marked complete"
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

log "docker login to ${HARBOR_HOST} ..."
echo "${HARBOR_ROBOT_TOKEN}" | docker login "${HARBOR_HOST}" -u "${HARBOR_ROBOT_USER}" --password-stdin \
  && log "docker login ok" \
  || { log "docker login failed"; exit 3; }

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
    log "tailnet ready; cloud runtime url: ${CLOUD_OPS_RUNTIME_BASE_URL}"
    set_kv "EDGE_API_BIND_ADDR" "$TAILNET_IP"
    log "edge-api bind address set to tailnet ip: ${TAILNET_IP}"
  else
    log "tailnet did not become ready; bootstrap will not fall back to public cloud url"
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
    log "license bind request ok"
  else
    log "license bind request failed; bootstrap will not be marked complete"
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
  log "local license verify ok"
else
  log "local license verify failed; bootstrap will not be marked complete"
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
log "bootstrap complete. sentinel written: $SENTINEL"
