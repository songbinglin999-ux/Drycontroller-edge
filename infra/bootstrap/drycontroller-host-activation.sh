#!/usr/bin/env bash
#
# Host-side activation helper.
#
# This runs outside Docker so formal Tailscale re-auth and license activation do
# not depend on the compose stack staying up. Formal re-auth is deliberately
# best-effort: if the one-time formal auth key fails, the bootstrap tailnet must
# remain online for retry/debug.
#
set -euo pipefail

ENV_FILE="${DRYCONTROLLER_TAILNET_ENV:-/etc/drycontroller/tailnet.env}"
STATE_FILE="${DRYCONTROLLER_ACTIVATION_STATE:-/var/lib/drycontroller/activation-state.json}"
DEVICE_JSON="${DRYCONTROLLER_DEVICE_JSON:-/var/lib/drycontroller/device.json}"
LICENSE_FILE_PATH="${LICENSE_FILE_PATH:-/etc/drycontroller/license/license.json}"
FACTORY_LICENSE_ARCHIVE_PATH="${FACTORY_LICENSE_ARCHIVE_PATH:-/var/lib/drycontroller/factory-license.json}"
LICENSE_PUBLIC_KEY_PATH="${LICENSE_PUBLIC_KEY_PATH:-/etc/drycontroller/keys/license.pub}"
EDGE_ENV_FILE="${EDGE_ENV_FILE:-}"
LOG_PREFIX="[drycontroller-activation]"

log() {
  printf '%s %s\n' "$LOG_PREFIX" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

[ "$(id -u)" -eq 0 ] || die "run as root"
[ -f "$ENV_FILE" ] || die "missing tailnet env: $ENV_FILE"

# shellcheck disable=SC1090
. "$ENV_FILE"

CLOUD_OPS_BASE_URL="${CLOUD_OPS_BASE_URL:-}"
TS_LOGIN_SERVER="${TS_LOGIN_SERVER:-}"
TS_HOSTNAME="${TS_HOSTNAME:-${ENROLL_NAME:-$(hostname)}}"
REQUESTED_TENANT_ID="${REQUESTED_TENANT_ID:-${TENANT_ID:-}}"
REQUESTED_LINE_ID="${REQUESTED_LINE_ID:-${LINE_ID:-}}"

[ -n "$CLOUD_OPS_BASE_URL" ] || die "CLOUD_OPS_BASE_URL is required"
[ -n "$TS_LOGIN_SERVER" ] || die "TS_LOGIN_SERVER is required"

command -v curl >/dev/null 2>&1 || die "curl is not installed"
command -v python3 >/dev/null 2>&1 || die "python3 is not installed"
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
    except Exception:
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

tailnet_ip() {
  tailscale ip -4 2>/dev/null | head -n 1 || true
}

tailnet_hostname() {
  tailscale status --json 2>/dev/null | python3 -c 'import json,sys; data=json.load(sys.stdin); print(data.get("Self", {}).get("HostName", ""))' 2>/dev/null || true
}

license_fingerprint() {
  ensure_factory_license_archive
  STATE_FILE="$STATE_FILE" python3 - "$FACTORY_LICENSE_ARCHIVE_PATH" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["STATE_FILE"])
state = {}
if state_path.exists():
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        state = {}

existing = (state.get("factory_license_fingerprint") or "").strip()
if existing:
    print(existing)
    raise SystemExit(0)

fingerprint = hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest()
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
  [ -f "$LICENSE_FILE_PATH" ] || die "missing factory license: $LICENSE_FILE_PATH"
  FACTORY_LICENSE_ARCHIVE_PATH="$FACTORY_LICENSE_ARCHIVE_PATH" python3 - "$LICENSE_FILE_PATH" <<'PY'
import json
import os
import sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(os.environ["FACTORY_LICENSE_ARCHIVE_PATH"])
payload = json.loads(src.read_text(encoding="utf-8"))
if not isinstance(payload, dict) or not payload.get("signature"):
    raise SystemExit("factory license is not a signed license JSON")
if payload.get("schema") != "drycontroller.factory-license.v1":
    raise SystemExit("license JSON is not a factory license")
dst.parent.mkdir(parents=True, exist_ok=True)
tmp = dst.with_suffix(dst.suffix + ".tmp")
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp.replace(dst)
PY
  chmod 0600 "$FACTORY_LICENSE_ARCHIVE_PATH" || true
}

machine_id() {
  cat /etc/machine-id 2>/dev/null || true
}

write_activation_payload() {
  ensure_factory_license_archive
  FACTORY_LICENSE_FINGERPRINT="$(license_fingerprint)" \
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
    "ts_hostname": resp.get("formal_ts_hostname") or resp.get("node_name"),
    "cloud_ops_base_url": os.environ.get("CLOUD_OPS_BASE_URL", ""),
    "activated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
}
existing = {}
path = Path(os.environ["DEVICE_JSON"])
if path.exists():
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
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
if not node_token:
    raise SystemExit(0)

path = Path(os.environ["ENV_FILE"])
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
replacement = f"CLOUD_OPS_ENROLL_TOKEN={shlex.quote(node_token)}"
out = []
replaced = False
for line in lines:
    if line.startswith("CLOUD_OPS_ENROLL_TOKEN="):
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
  chmod 0600 "$ENV_FILE" || true
}

sync_tailnet_env_after_formal_reauth() {
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
    if line.startswith("TS_AUTHKEY="):
        continue
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

sync_edge_env_after_formal_reauth() {
  local ip="$1"
  local hostname="$2"
  [ -n "${EDGE_ENV_FILE:-}" ] || {
    log "EDGE_ENV_FILE not set; skipping edge .env tailnet binding sync"
    return 0
  }
  EDGE_ENV_FILE="$EDGE_ENV_FILE" FORMAL_TAILNET_IP="$ip" FORMAL_HOSTNAME="$hostname" python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["EDGE_ENV_FILE"])
if not path.exists():
    raise SystemExit(0)
updates = {}
ip = os.environ.get("FORMAL_TAILNET_IP", "").strip()
hostname = os.environ.get("FORMAL_HOSTNAME", "").strip()
if ip:
    updates["EDGE_API_BIND_ADDR"] = ip
if hostname:
    updates["TS_HOSTNAME"] = hostname
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
  command -v docker >/dev/null 2>&1 || return 0
  log "re-applying edge compose binding after formal tailnet re-auth"
  if ! (cd "$EDGE_ROOT" && docker compose up -d); then
    log "edge compose re-apply failed; bootstrap or operator must run docker compose up -d"
  fi
}

# Factory-activation re-auth path. The legacy path has a parallel implementation in
# drycontroller-bootstrap.sh:attempt_formal_tailnet_reauth. Keep the failure semantics
# (preserve bootstrap tailnet, never break the survival path) in sync between the two.
attempt_formal_tailnet_reauth() {
  local authkey="$1"
  local hostname="$2"

  [ -n "$authkey" ] || return 0
  [ -n "$hostname" ] || hostname="$TS_HOSTNAME"

  local before_ip after_ip
  before_ip="$(tailnet_ip)"
  log "attempting formal tailnet re-auth hostname=${hostname}"
  if tailscale up --authkey="${authkey}" --login-server="${TS_LOGIN_SERVER}" --hostname="${hostname}"; then
    after_ip="$(tailnet_ip)"
    [ -n "$after_ip" ] || {
      log "formal re-auth returned success but no tailnet IPv4 is available; keeping bootstrap auth material"
      return 1
    }
    sync_tailnet_env_after_formal_reauth "$hostname"
    sync_edge_env_after_formal_reauth "$after_ip" "$hostname"
    reapply_edge_compose_binding
    log "formal tailnet re-auth ok ip=${after_ip}; bootstrap TS_AUTHKEY removed from ${ENV_FILE}"
    return 0
  fi

  log "formal tailnet re-auth failed; preserving bootstrap tailnet for retry"
  after_ip="$(tailnet_ip)"
  if [ -z "$after_ip" ] && [ -n "$before_ip" ] && command -v systemctl >/dev/null 2>&1; then
    log "tailnet IP disappeared after failed re-auth; restarting drycontroller-tailnet.service"
    systemctl restart drycontroller-tailnet.service || true
  fi
  return 1
}

response_file="$(mktemp)"
trap 'rm -f "$response_file"' EXIT

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

install_license_from_response "$response_file"
save_device_identity "$response_file"
save_runtime_credentials "$response_file"
write_state "cloud_activated" "production_license_installed"

formal_authkey="$(python3 - "$response_file" <<'PY'
import json, sys
print((json.load(open(sys.argv[1], encoding="utf-8")).get("formal_ts_authkey") or "").strip())
PY
)"
formal_hostname="$(python3 - "$response_file" <<'PY'
import json, sys
resp = json.load(open(sys.argv[1], encoding="utf-8"))
print((resp.get("formal_ts_hostname") or resp.get("node_name") or "").strip())
PY
)"

if [ -n "$formal_authkey" ]; then
  if attempt_formal_tailnet_reauth "$formal_authkey" "$formal_hostname"; then
    write_state "formal_tailnet_joined" "formal_tailnet_reauth_ok"
    write_state "formal_license_active" "activation_complete"
    log "activation complete"
  else
    write_state "cloud_activated" "formal_tailnet_reauth_failed_bootstrap_tailnet_preserved"
    log "production license installed; formal tailnet re-auth failed, bootstrap tailnet preserved"
  fi
else
  log "cloud activation response did not include formal_ts_authkey; bootstrap tailnet stays active"
  write_state "cloud_activated" "production_license_installed_formal_tailnet_pending"
  log "production license installed; formal tailnet re-auth pending"
fi
