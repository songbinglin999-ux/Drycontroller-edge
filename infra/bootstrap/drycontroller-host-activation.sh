#!/usr/bin/env bash
#
# Host-side activation helper.
#
# This runs outside Docker so license activation and tailnet binding do not
# depend on the compose stack staying up. Tailscale auth keys are bootstrap-only:
# once Cloud Ops approves the license, the current tailnet node is bound to the
# Cloud Ops identity without a second auth key.
#
set -euo pipefail

ENV_FILE="${DRYCONTROLLER_TAILNET_ENV:-/etc/drycontroller/tailnet.env}"
BUNDLE_FILE="${DRYCONTROLLER_ACTIVATION_BUNDLE:-/etc/drycontroller/activation-bundle.json}"
STATE_FILE="${DRYCONTROLLER_ACTIVATION_STATE:-/var/lib/drycontroller/activation-state.json}"
DEVICE_JSON="${DRYCONTROLLER_DEVICE_JSON:-/var/lib/drycontroller/device.json}"
BOOTSTRAP_SENTINEL="${DRYCONTROLLER_BOOTSTRAP_SENTINEL:-/var/lib/drycontroller/.bootstrapped}"
LICENSE_FILE_PATH="${LICENSE_FILE_PATH:-/etc/drycontroller/license/license.json}"
FACTORY_LICENSE_ARCHIVE_PATH="${FACTORY_LICENSE_ARCHIVE_PATH:-/var/lib/drycontroller/factory-license.json}"
LICENSE_PUBLIC_KEY_PATH="${LICENSE_PUBLIC_KEY_PATH:-/etc/drycontroller/keys/license.pub}"
EDGE_ENV_FILE="${EDGE_ENV_FILE:-}"
START_ACTIVE_PROGRAM="${DRYCONTROLLER_START_ACTIVE_PROGRAM:-/usr/libexec/drycontroller/start-active-release}"
LOG_PREFIX="[drycontroller-activation]"

log() {
  printf '%s %s\n' "$LOG_PREFIX" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

[ "$(id -u)" -eq 0 ] || die "run as root"
if [ ! -f "$BUNDLE_FILE" ]; then
  log "no activation bundle; skipping activation"
  exit 0
fi

command -v python3 >/dev/null 2>&1 || die "python3 is not installed"

materialize_activation_bundle() {
  [ -f "$BUNDLE_FILE" ] || return 0

  BUNDLE_FILE="$BUNDLE_FILE" \
  ENV_FILE="$ENV_FILE" \
  LICENSE_FILE_PATH="$LICENSE_FILE_PATH" \
  FACTORY_LICENSE_ARCHIVE_PATH="$FACTORY_LICENSE_ARCHIVE_PATH" \
  python3 - <<'PY'
import json
import os
import shlex
from pathlib import Path

bundle = json.loads(Path(os.environ["BUNDLE_FILE"]).read_text(encoding="utf-8"))
if bundle.get("schema") != "drycontroller.activation-bundle.v1":
    raise SystemExit("activation bundle is not drycontroller.activation-bundle.v1")

tailnet = bundle.get("tailnet")
if not isinstance(tailnet, dict):
    raise SystemExit("activation bundle missing tailnet object")
aliases = {
    "ts_login_server": "TS_LOGIN_SERVER",
    "ts_hostname": "TS_HOSTNAME",
    "ts_authkey": "TS_AUTHKEY",
    "cloud_ops_base_url": "CLOUD_OPS_BASE_URL",
}
values = {}
for raw_key, raw_value in tailnet.items():
    key = aliases.get(str(raw_key), str(raw_key)).upper()
    if raw_value is not None:
        values[key] = str(raw_value)
required = {"TS_LOGIN_SERVER", "TS_HOSTNAME", "TS_AUTHKEY", "CLOUD_OPS_BASE_URL"}
missing = sorted(key for key in required if not values.get(key))
if missing:
    raise SystemExit(f"activation bundle tailnet missing: {', '.join(missing)}")

env_path = Path(os.environ["ENV_FILE"])
if not env_path.exists():
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        "\n".join(f"{key}={shlex.quote(value)}" for key, value in sorted(values.items())) + "\n",
        encoding="utf-8",
    )

factory_license = bundle.get("factory_license")
if not isinstance(factory_license, dict):
    raise SystemExit("activation bundle missing factory_license object")
if factory_license.get("schema") != "drycontroller.factory-license.v1":
    raise SystemExit("activation bundle factory_license is not drycontroller.factory-license.v1")
if not factory_license.get("signature"):
    raise SystemExit("activation bundle factory_license missing signature")

archive_path = Path(os.environ["FACTORY_LICENSE_ARCHIVE_PATH"])
if not archive_path.exists():
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(json.dumps(factory_license, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

PY
  chmod 0600 "$ENV_FILE" "$FACTORY_LICENSE_ARCHIVE_PATH" 2>/dev/null || true
  log "materialized activation files from activation bundle"
}

materialize_activation_bundle
[ -f "$ENV_FILE" ] || die "missing tailnet env: $ENV_FILE"

# shellcheck disable=SC1090
. "$ENV_FILE"

CLOUD_OPS_BASE_URL="${CLOUD_OPS_BASE_URL:-}"
TS_LOGIN_SERVER="${TS_LOGIN_SERVER:-}"
TS_HOSTNAME="${TS_HOSTNAME:-${ENROLL_NAME:-$(hostname)}}"
EDGE_ROOT="${EDGE_ROOT:-/opt/Drycontroller-edge}"
EDGE_ENV_FILE="${EDGE_ENV_FILE:-${EDGE_ROOT}/.env}"
REQUESTED_TENANT_ID="${REQUESTED_TENANT_ID:-${TENANT_ID:-}}"
REQUESTED_LINE_ID="${REQUESTED_LINE_ID:-${LINE_ID:-}}"

[ -n "$CLOUD_OPS_BASE_URL" ] || die "CLOUD_OPS_BASE_URL is required"
[ -n "$TS_LOGIN_SERVER" ] || die "TS_LOGIN_SERVER is required"

command -v curl >/dev/null 2>&1 || die "curl is not installed"
command -v tailscale >/dev/null 2>&1 || die "tailscale CLI is not installed"

write_state() {
  local state="$1"
  local message="${2:-}"
  mkdir -p "$(dirname "$STATE_FILE")"
  STATE_FILE="$STATE_FILE" STATE="$state" MESSAGE="$message" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

path = Path(os.environ["STATE_FILE"])
payload = {}
if path.exists():
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
payload["state"] = os.environ["STATE"]
payload["message"] = os.environ.get("MESSAGE") or payload.get("message") or ""
payload["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp.replace(path)
PY
  chmod 0600 "$STATE_FILE" || true
}

mark_bootstrap_complete() {
  mkdir -p "$(dirname "$BOOTSTRAP_SENTINEL")"
  : > "$BOOTSTRAP_SENTINEL"
  chmod 0600 "$BOOTSTRAP_SENTINEL" || true
}

tailnet_ip() {
  tailscale ip -4 2>/dev/null | head -n 1 || true
}

tailnet_hostname() {
  tailscale status --json 2>/dev/null | python3 -c 'import json,sys; data=json.load(sys.stdin); print(data.get("Self", {}).get("HostName", ""))' 2>/dev/null || true
}

license_fingerprint() {
  ensure_factory_license_archive
  STATE_FILE="$STATE_FILE" MACHINE_ID="$(machine_id)" python3 - "$FACTORY_LICENSE_ARCHIVE_PATH" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["STATE_FILE"])
machine_id = os.environ.get("MACHINE_ID", "").strip()
state = {}
if state_path.exists():
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        state = {}

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
h = hashlib.sha256()
h.update(raw)
h.update(b"\0")
h.update(machine_id.encode("utf-8"))
fingerprint = h.hexdigest()
state["factory_license_fingerprint"] = fingerprint
state_path.parent.mkdir(parents=True, exist_ok=True)
tmp = state_path.with_suffix(state_path.suffix + ".tmp")
tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp.replace(state_path)
print(fingerprint)
PY
  chmod 0600 "$STATE_FILE" || true
}

ensure_factory_license_archive() {
  if [ -f "$FACTORY_LICENSE_ARCHIVE_PATH" ]; then
    return 0
  fi
  die "missing factory license archive: $FACTORY_LICENSE_ARCHIVE_PATH; activation bundle must materialize it"
}

machine_id() {
  cat /etc/machine-id 2>/dev/null || true
}

write_activation_payload() {
  ensure_factory_license_archive
  MACHINE_ID="$(machine_id)" \
  BOOTSTRAP_TS_NODE="$(tailnet_hostname)" \
  BOOTSTRAP_TS_IP="$(tailnet_ip)" \
  REQUESTED_TENANT_ID="$REQUESTED_TENANT_ID" \
  REQUESTED_LINE_ID="$REQUESTED_LINE_ID" \
  python3 - "$FACTORY_LICENSE_ARCHIVE_PATH" <<'PY'
import json
import os
import sys
from pathlib import Path

payload = {
    "factory_license": json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")),
    "factory_license_fingerprint": os.environ["FACTORY_LICENSE_FINGERPRINT"],
    "machine_id": os.environ.get("MACHINE_ID", "").strip(),
    "bootstrap_ts_node": os.environ.get("BOOTSTRAP_TS_NODE", "").strip(),
    "bootstrap_ts_ip": os.environ.get("BOOTSTRAP_TS_IP", "").strip() or None,
    "requested_tenant_id": os.environ.get("REQUESTED_TENANT_ID", "").strip() or None,
    "requested_line_id": os.environ.get("REQUESTED_LINE_ID", "").strip() or None,
}
if not payload["machine_id"]:
    raise SystemExit("missing machine_id")
if not payload["bootstrap_ts_node"]:
    payload["bootstrap_ts_node"] = os.uname().nodename
print(json.dumps(payload, ensure_ascii=False))
PY
}

install_license_from_response() {
  local response_file="$1"
  ensure_factory_license_archive
  mkdir -p "$(dirname "$LICENSE_FILE_PATH")" "$(dirname "$LICENSE_PUBLIC_KEY_PATH")"
  python3 - "$response_file" "$LICENSE_FILE_PATH" "$LICENSE_PUBLIC_KEY_PATH" <<'PY'
import json
import sys
from pathlib import Path

resp = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
license_payload = resp.get("license_payload") or resp.get("license") or resp.get("payload")
public_key = (resp.get("license_public_key") or "").strip()
if license_payload is None and not public_key:
    if Path(sys.argv[2]).is_file() and Path(sys.argv[3]).is_file():
        raise SystemExit(0)
    raise SystemExit("activation response omitted license and no local license is installed")
if not isinstance(license_payload, dict) or not license_payload.get("signature"):
    raise SystemExit("activation response missing signed license")
if not public_key:
    raise SystemExit("activation response missing license_public_key")
Path(sys.argv[2]).write_text(json.dumps(license_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
Path(sys.argv[3]).write_text(public_key + "\n", encoding="utf-8")
PY
  chmod 0640 "$LICENSE_FILE_PATH" "$LICENSE_PUBLIC_KEY_PATH" || true
}

save_device_identity() {
  local response_file="$1"
  mkdir -p "$(dirname "$DEVICE_JSON")"
  MACHINE_ID="$(machine_id)" DEVICE_JSON="$DEVICE_JSON" python3 - "$response_file" <<'PY'
import datetime as dt
import json
import os
import sys
from pathlib import Path

resp = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
payload = {
    "device_id": resp.get("device_id"),
    "tenant_id": resp.get("tenant_id"),
    "line_id": resp.get("line_id"),
    "node_name": resp.get("node_name"),
    "machine_id": os.environ.get("MACHINE_ID", ""),
    "ts_hostname": resp.get("node_name"),
    "cloud_ops_base_url": os.environ.get("CLOUD_OPS_BASE_URL", ""),
    "activated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
}
existing = {}
path = Path(os.environ["DEVICE_JSON"])
if path.exists():
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        existing = {}
existing.update({k: v for k, v in payload.items() if v})
path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  chmod 0600 "$DEVICE_JSON" || true
}

save_runtime_credentials() {
  local response_file="$1"
  ENV_FILE="$ENV_FILE" python3 - "$response_file" <<'PY'
import json
import os
import shlex
import sys
from pathlib import Path

resp = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
node_token = (resp.get("node_enroll_token") or resp.get("enroll_token") or "").strip()
updates = {}
if node_token:
    updates["CLOUD_OPS_ENROLL_TOKEN"] = node_token
if (resp.get("tenant_id") or "").strip():
    updates["TENANT_ID"] = resp["tenant_id"].strip()
if (resp.get("line_id") or "").strip():
    updates["LINE_ID"] = resp["line_id"].strip()
if not updates:
    raise SystemExit(0)

path = Path(os.environ["ENV_FILE"])
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
out = []
seen = set()
for line in lines:
    key = line.split("=", 1)[0]
    if key in updates:
        if key not in seen:
            out.append(f"{key}={shlex.quote(updates[key])}")
            seen.add(key)
        continue
    out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={shlex.quote(value)}")
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
tmp.replace(path)
PY
  chmod 0600 "$ENV_FILE" || true
}

save_edge_ops_credentials() {
  local response_file="$1"
  [ -n "${EDGE_ENV_FILE:-}" ] || return 0
  EDGE_ENV_FILE="$EDGE_ENV_FILE" python3 - "$response_file" <<'PY'
import json
import os
import sys
from pathlib import Path

resp = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
admin_key = (resp.get("edge_admin_api_key") or "").strip()
control_key = (resp.get("edge_control_api_key") or "").strip()
updates = {}
if admin_key:
    updates["ADMIN_API_KEYS"] = json.dumps([admin_key], separators=(",", ":"))
if control_key:
    updates["CONTROL_API_KEYS"] = json.dumps([control_key], separators=(",", ":"))
if not updates:
    raise SystemExit(0)

path = Path(os.environ["EDGE_ENV_FILE"])
if not path.exists():
    raise SystemExit(0)
lines = path.read_text(encoding="utf-8").splitlines()
out = []
seen = set()
for line in lines:
    key = line.split("=", 1)[0]
    if key in updates:
        if key not in seen:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        continue
    out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
tmp.replace(path)
PY
  chmod 0600 "$EDGE_ENV_FILE" || true
}

sync_tailnet_env_after_activation() {
  local hostname="$1"
  ENV_FILE="$ENV_FILE" FORMAL_HOSTNAME="$hostname" python3 - <<'PY'
import os
import shlex
from pathlib import Path

path = Path(os.environ["ENV_FILE"])
hostname = os.environ.get("FORMAL_HOSTNAME", "").strip()
lines = path.read_text(encoding="utf-8").splitlines()
updates = {}
if hostname:
    updates["TS_HOSTNAME"] = hostname
    updates["ENROLL_NAME"] = hostname
out = []
seen = set()
for line in lines:
    key = line.split("=", 1)[0]
    if key in updates:
        if key not in seen:
            out.append(f"{key}={shlex.quote(updates[key])}")
            seen.add(key)
        continue
    out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={shlex.quote(value)}")
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
tmp.replace(path)
PY
  chmod 0600 "$ENV_FILE" || true
}

remove_bootstrap_tailnet_authkey() {
  ENV_FILE="$ENV_FILE" python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["ENV_FILE"])
if not path.exists():
    raise SystemExit(0)
lines = path.read_text(encoding="utf-8").splitlines()
kept = [line for line in lines if not line.startswith("TS_AUTHKEY=")]
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
tmp.replace(path)
PY
  chmod 0600 "$ENV_FILE" || true
}

sync_edge_env_after_activation() {
  local ip="$1"
  local hostname="$2"
  [ -n "${EDGE_ENV_FILE:-}" ] || {
    log "EDGE_ENV_FILE not set; skipping edge .env tailnet binding sync"
    return 0
  }
  EDGE_ENV_FILE="$EDGE_ENV_FILE" ENV_FILE="$ENV_FILE" FORMAL_TAILNET_IP="$ip" FORMAL_HOSTNAME="$hostname" python3 - <<'PY'
import os
import shlex
from pathlib import Path

def read_env(path: Path) -> dict[str, str]:
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError:
            continue
        if len(tokens) == 1 and "=" in tokens[0]:
            key, value = tokens[0].split("=", 1)
            values[key] = value
    return values

path = Path(os.environ["EDGE_ENV_FILE"])
if not path.exists():
    raise SystemExit(0)
updates = {}
ip = os.environ.get("FORMAL_TAILNET_IP", "").strip()
hostname = os.environ.get("FORMAL_HOSTNAME", "").strip()
host_env = read_env(Path(os.environ["ENV_FILE"]))
if ip:
    updates["EDGE_API_BIND_ADDR"] = ip
if hostname:
    updates["TS_HOSTNAME"] = hostname
for key in ("TS_LOGIN_SERVER", "CLOUD_OPS_BASE_URL", "CLOUD_OPS_ENROLL_TOKEN", "CLOUD_OPS_ENROLL_TOKEN_HEADER"):
    value = host_env.get(key, "").strip()
    if value:
        updates[key] = value
if not updates:
    raise SystemExit(0)
lines = path.read_text(encoding="utf-8").splitlines()
out = []
seen = set()
for line in lines:
    key = line.split("=", 1)[0]
    if key in updates:
        if key not in seen:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        continue
    out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
tmp.replace(path)
PY
  log "edge .env tailnet binding synced ip=${ip:-?} hostname=${hostname:-?}"
}

reapply_edge_compose_binding() {
  [ -n "${EDGE_ROOT:-}" ] || return 0
  [ -f "${EDGE_ROOT}/docker-compose.yml" ] || return 0
  [ -x "$START_ACTIVE_PROGRAM" ] || {
    log "signed active release starter is unavailable: $START_ACTIVE_PROGRAM"
    return 1
  }
  log "starting signed active release after activation tailnet sync"
  if ! "$START_ACTIVE_PROGRAM"; then
    log "signed active release start failed; activation will retry"
    return 1
  fi
}

start_host_enroll_service_if_configured() {
  [ -f "$ENV_FILE" ] || return 0
  grep -q '^CLOUD_OPS_ENROLL_TOKEN=' "$ENV_FILE" || {
    log "node enroll token unavailable; drycontroller-enroll.service remains stopped"
    return 0
  }
  command -v systemctl >/dev/null 2>&1 || return 0
  log "starting host enroll service after activation"
  systemctl enable drycontroller-enroll.service >/dev/null 2>&1 || true
  if ! systemctl restart drycontroller-enroll.service; then
    log "drycontroller-enroll.service start failed; operator must inspect enroll logs"
  fi
}

confirm_current_tailnet_binding() {
  local ip="$1"
  local hostname="$2"
  local confirm_url="${CLOUD_OPS_BASE_URL%/}/api/v1/factory-activation/${FACTORY_LICENSE_FINGERPRINT}/bind-tailnet-node"
  local confirm_payload
  confirm_payload="$(FACTORY_LICENSE_PATH="$FACTORY_LICENSE_ARCHIVE_PATH" MACHINE_ID="$(machine_id)" TAILNET_IP="$ip" TAILNET_HOSTNAME="$hostname" python3 - <<'PY'
import json
import os
from pathlib import Path

payload = {
    "factory_license": json.loads(Path(os.environ["FACTORY_LICENSE_PATH"]).read_text(encoding="utf-8")),
    "machine_id": os.environ.get("MACHINE_ID", "").strip(),
    "tailnet_ip": os.environ.get("TAILNET_IP", "").strip() or None,
    "tailnet_hostname": os.environ.get("TAILNET_HOSTNAME", "").strip() or None,
}
print(json.dumps(payload, ensure_ascii=False))
PY
)"
  if curl -fsS -H "Content-Type: application/json" -d "$confirm_payload" -o /dev/null "$confirm_url"; then
    log "current tailnet node bound with Cloud Ops"
    return 0
  else
    log "tailnet node bind confirm failed; keeping local activation state"
    return 1
  fi
}

rename_current_tailnet_node() {
  local hostname="$1"
  [ -n "$hostname" ] || hostname="$TS_HOSTNAME"
  [ -n "$hostname" ] || return 0

  if tailscale set --hostname="${hostname}"; then
    log "tailnet hostname set to ${hostname}"
  else
    log "tailnet hostname rename failed; continuing with Cloud Ops license-bound node identity"
  fi
}

bind_current_tailnet_node() {
  local hostname="$1"
  [ -n "$hostname" ] || hostname="${TS_HOSTNAME:-$(hostname)}"

  rename_current_tailnet_node "$hostname"
  sync_tailnet_env_after_activation "$hostname"

  local current_ip
  current_ip="$(tailnet_ip)"
  [ -n "$current_ip" ] || {
    log "current tailnet node has no IPv4; cannot bind license identity yet"
    return 1
  }
  sync_edge_env_after_activation "$current_ip" "$hostname"
  reapply_edge_compose_binding || return 1
  if ! confirm_current_tailnet_binding "$current_ip" "$hostname"; then
    return 75
  fi
  remove_bootstrap_tailnet_authkey
  log "current tailnet node bound as ${hostname} ip=${current_ip}; bootstrap TS_AUTHKEY removed from ${ENV_FILE}"
  return 0
}

response_file="$(mktemp)"
trap 'rm -f "$response_file"' EXIT

FACTORY_LICENSE_FINGERPRINT="$(license_fingerprint)"
export FACTORY_LICENSE_FINGERPRINT

current_state="$(python3 - "$STATE_FILE" <<'PY'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print("")
    raise SystemExit(0)
try:
    print((json.loads(path.read_text(encoding="utf-8")).get("state") or "").strip())
except (OSError, UnicodeDecodeError, json.JSONDecodeError):
    print("")
PY
)"
if [ "$current_state" = "formal_license_active" ]; then
  sync_edge_env_after_activation "$(tailnet_ip)" "${TS_HOSTNAME:-}"
  reapply_edge_compose_binding || exit 75
  mark_bootstrap_complete
  start_host_enroll_service_if_configured
  log "activation already complete"
  exit 0
fi
if [ "$current_state" = "formal_tailnet_joined" ]; then
  current_tailnet_ip="$(tailnet_ip)"
  if confirm_current_tailnet_binding "$current_tailnet_ip" "${TS_HOSTNAME:-}"; then
    sync_edge_env_after_activation "$current_tailnet_ip" "${TS_HOSTNAME:-}"
    reapply_edge_compose_binding || exit 75
    write_state "formal_license_active" "activation_complete"
    mark_bootstrap_complete
    start_host_enroll_service_if_configured
    log "activation complete"
    exit 0
  fi
  write_state "formal_tailnet_joined" "tailnet_node_bind_confirm_failed"
  exit 75
fi

write_state "bootstrap_tailnet_joined" "calling_cloud_activation"
activation_url="${CLOUD_OPS_BASE_URL%/}/api/v1/factory-activation"
log "calling cloud activation: ${activation_url}"
if ! curl -fsS \
  -H "Content-Type: application/json" \
  -d "$(write_activation_payload)" \
  -o "$response_file" \
  "$activation_url"; then
  write_state "bootstrap_tailnet_joined" "cloud_activation_failed"
  die "cloud activation request failed"
fi

response_state="$(python3 - "$response_file" <<'PY'
import json, sys
print((json.load(open(sys.argv[1], encoding="utf-8")).get("state") or "").strip())
PY
)"
if [ "$response_state" = "pending_ops_approval" ]; then
  write_state "pending_ops_approval" "waiting_for_ops_approval"
  log "factory activation is pending ops approval; bootstrap tailnet preserved"
  exit 75
fi

install_license_from_response "$response_file"
save_device_identity "$response_file"
save_runtime_credentials "$response_file"
save_edge_ops_credentials "$response_file"
write_state "cloud_activated" "production_license_installed"

node_hostname="$(python3 - "$response_file" <<'PY'
import json, sys
resp = json.load(open(sys.argv[1], encoding="utf-8"))
print((resp.get("node_name") or "").strip())
PY
)"

set +e
bind_current_tailnet_node "$node_hostname"
bind_status=$?
set -e
if [ "$bind_status" -eq 0 ]; then
  write_state "formal_license_active" "activation_complete"
  mark_bootstrap_complete
  start_host_enroll_service_if_configured
  log "activation complete"
elif [ "$bind_status" -eq 75 ]; then
  write_state "cloud_activated" "tailnet_node_bind_confirm_failed"
  exit 75
else
  write_state "cloud_activated" "tailnet_node_bind_failed"
  log "production license installed; current tailnet node bind failed, activation will retry"
  exit 75
fi
