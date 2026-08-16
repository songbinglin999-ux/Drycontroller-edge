#!/usr/bin/env bash
#
# Factory preflight for Drycontroller edge nodes.
#
# This script is intentionally local and read-only: it checks bootstrap assets,
# license verification, compose/container state, readiness endpoints, and basic
# cloud/tailnet configuration before a machine leaves the factory.
#
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR" || exit 2

ALLOW_DEGRADED=true
CHECK_OTA=auto
TIMEOUT_SEC="${PREFLIGHT_TIMEOUT_SEC:-5}"

usage() {
  cat <<'EOF'
Usage:
  bash scripts/preflight.sh [--strict] [--check-ota|--skip-ota]

Options:
  --strict      Treat degraded checks as failure.
  --check-ota   Require root-owned OTA helper readiness.
  --skip-ota    Do not check root-owned OTA helper readiness.

Output:
  JSON only. Exit code 0 means pass or accepted degraded. Exit code 1 means fail.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --strict)
      ALLOW_DEGRADED=false
      ;;
    --check-ota)
      CHECK_OTA=true
      ;;
    --skip-ota)
      CHECK_OTA=false
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT_DIR/.env"
  set +a
fi

STATE_DIR="${PREFLIGHT_STATE_DIR:-/var/lib/drycontroller}"
SENTINEL="${PREFLIGHT_BOOTSTRAP_SENTINEL:-${STATE_DIR}/.bootstrapped}"
DEVICE_JSON="${PREFLIGHT_DEVICE_JSON:-${STATE_DIR}/device.json}"
LICENSE_FILE="${LICENSE_FILE_PATH:-/etc/drycontroller/license/license.json}"
LICENSE_TRUST_POLICY="${LICENSE_TRUST_POLICY_PATH:-/etc/drycontroller/keys/license-trust/policy.json}"
COSIGN_PUBKEY="${OTA_COSIGN_PUBKEY_HOST_PATH:-/etc/drycontroller/keys/cosign.pub}"
if [ ! -f "$COSIGN_PUBKEY" ]; then
  COSIGN_PUBKEY="${ROOT_DIR}/configs/keys/cosign.pub"
fi

EDGE_API_PORT="${EDGE_API_LOCAL_PORT:-18080}"
EDGE_API_READY_HOST="${PREFLIGHT_EDGE_API_HOST:-127.0.0.1}"
case "$EDGE_API_READY_HOST" in
  ""|"0.0.0.0"|"::")
    EDGE_API_READY_HOST="127.0.0.1"
    ;;
esac
EDGE_DAQ_HEALTH_PORT="${EDGE_DAQ_HEALTH_PORT:-8082}"
LICENSE_SERVICE_PORT="${LICENSE_SERVICE_PORT:-8004}"
EDGE_MONITOR_PORT="${EDGE_MONITOR_PORT:-8080}"
OTA_HELPER_SOCKET=/run/drycontroller-ota/helper.sock

CHECKS_FILE="$(mktemp "${TMPDIR:-/tmp}/drycontroller-preflight.XXXXXX")"
trap 'rm -f "$CHECKS_FILE"' EXIT

add_check() {
  local id="$1"
  local status="$2"
  local reason="${3:-}"
  local hint="${4:-}"
  local detail="${5-}"
  if [ -z "$detail" ]; then
    detail="{}"
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$id" "$status" "$reason" "$hint" "$detail" >> "$CHECKS_FILE"
}

json_detail() {
  python3 - "$@" <<'PY'
import json
import sys

pairs = sys.argv[1:]
obj = {}
for i in range(0, len(pairs), 2):
    key = pairs[i]
    value = pairs[i + 1] if i + 1 < len(pairs) else ""
    obj[key] = value
print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
PY
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  else
    return 127
  fi
}

check_file_exists() {
  local id="$1"
  local path="$2"
  local hint="$3"
  if [ -f "$path" ]; then
    add_check "$id" pass ok "" "$(json_detail path "$path")"
  else
    add_check "$id" fail missing_file "$hint" "$(json_detail path "$path")"
  fi
}

check_device_identity() {
  if [ ! -f "$DEVICE_JSON" ]; then
    add_check "device.identity" fail missing_device_json "bootstrap should create ${DEVICE_JSON}" "$(json_detail path "$DEVICE_JSON")"
    return
  fi
  local result
  result="$(python3 - "$DEVICE_JSON" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
    print(json.dumps({"ok": False, "reason": "invalid_json", "error": str(exc)}, ensure_ascii=False))
    raise SystemExit(0)

required = ["device_id", "tenant_id", "node_name"]
missing = [k for k in required if not data.get(k)]
print(json.dumps({
    "ok": not missing,
    "reason": "ok" if not missing else "missing_required_fields",
    "missing": missing,
    "device_id": data.get("device_id", ""),
    "tenant_id": data.get("tenant_id", ""),
    "node_name": data.get("node_name", ""),
}, ensure_ascii=False, separators=(",", ":")))
PY
)"
  if python3 -c 'import json,sys; raise SystemExit(0 if json.loads(sys.argv[1]).get("ok") else 1)' "$result"; then
    add_check "device.identity" pass ok "" "$result"
  else
    add_check "device.identity" fail "$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("reason","invalid"))' "$result")" "device.json must contain device_id/tenant_id/node_name" "$result"
  fi
}

http_check() {
  local id="$1"
  local url="$2"
  local degraded_ok="$3"
  local tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/drycontroller-http.XXXXXX")"
  local code
  if ! code="$(curl -sS -m "$TIMEOUT_SEC" -o "$tmp" -w "%{http_code}" "$url" 2>/dev/null)"; then
    code="000"
  fi
  local body
  body="$(tr -d '\n' < "$tmp" | cut -c1-500)"
  rm -f "$tmp"
  local detail
  detail="$(json_detail url "$url" http_code "$code" body "$body")"
  case "$code" in
    2??)
      add_check "$id" pass ok "" "$detail"
      ;;
    503)
      if [ "$degraded_ok" = true ] && [ "$ALLOW_DEGRADED" = true ]; then
        add_check "$id" degraded service_degraded "accepted in factory mode; run --strict for final acceptance" "$detail"
      else
        add_check "$id" fail service_degraded "readiness endpoint returned 503" "$detail"
      fi
      ;;
    000)
      add_check "$id" fail unreachable "endpoint is not reachable" "$detail"
      ;;
    *)
      add_check "$id" fail "http_${code}" "unexpected HTTP status" "$detail"
      ;;
  esac
}

container_http_check() {
  local id="$1"
  local container="$2"
  local url="$3"
  local degraded_ok="$4"
  local output
  output="$(docker exec "$container" python - "$url" "$TIMEOUT_SEC" <<'PY' 2>/dev/null || true
import json
import sys
import urllib.error
import urllib.request

url = sys.argv[1]
timeout = float(sys.argv[2])
try:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        body = resp.read(800).decode("utf-8", "replace")
        print(json.dumps({"http_code": resp.status, "body": body}, ensure_ascii=False))
except urllib.error.HTTPError as exc:
    body = exc.read(800).decode("utf-8", "replace")
    print(json.dumps({"http_code": exc.code, "body": body}, ensure_ascii=False))
except (OSError, TimeoutError, urllib.error.URLError) as exc:
    print(json.dumps({"http_code": 0, "body": "", "error": str(exc)}, ensure_ascii=False))
PY
)"
  if [ -z "$output" ]; then
    add_check "$id" fail container_exec_failed "failed to execute readiness probe inside ${container}" "$(json_detail container "$container" url "$url")"
    return
  fi
  local code body detail
  code="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("http_code",0))' "$output")"
  body="$(python3 -c 'import json,sys; print(str(json.loads(sys.argv[1]).get("body","")).replace("\n","")[:500])' "$output")"
  detail="$(json_detail container "$container" url "$url" http_code "$code" body "$body")"
  case "$code" in
    2??)
      add_check "$id" pass ok "" "$detail"
      ;;
    503)
      if [ "$degraded_ok" = true ] && [ "$ALLOW_DEGRADED" = true ]; then
        add_check "$id" degraded service_degraded "accepted in factory mode; run --strict for final acceptance" "$detail"
      else
        add_check "$id" fail service_degraded "readiness endpoint returned 503" "$detail"
      fi
      ;;
    0)
      add_check "$id" fail unreachable "endpoint is not reachable from inside ${container}" "$detail"
      ;;
    *)
      add_check "$id" fail "http_${code}" "unexpected HTTP status" "$detail"
      ;;
  esac
}

check_license_verify() {
  if ! has_cmd curl; then
    add_check "license.verify" fail missing_curl "install curl to run local license verification" "{}"
    return
  fi
  local tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/drycontroller-license.XXXXXX")"
  local url="http://127.0.0.1:${LICENSE_SERVICE_PORT}/verify"
  local code
  if ! code="$(curl -sS -m "$TIMEOUT_SEC" -o "$tmp" -w "%{http_code}" "$url" 2>/dev/null)"; then
    code="000"
  fi
  local body
  body="$(tr -d '\n' < "$tmp" | cut -c1-800)"
  rm -f "$tmp"
  local detail
  detail="$(json_detail url "$url" http_code "$code" body "$body")"
  if [ "$code" != "200" ]; then
    add_check "license.verify" fail "http_${code}" "license-service /verify must return HTTP 200 and valid=true" "$detail"
    return
  fi
  if python3 - "$body" <<'PY'
import json
import sys
try:
    data = json.loads(sys.argv[1])
except json.JSONDecodeError:
    raise SystemExit(1)
raise SystemExit(0 if data.get("valid") is True else 1)
PY
  then
    add_check "license.verify" pass ok "" "$detail"
  else
    add_check "license.verify" fail invalid_license "license-service returned valid=false" "$detail"
  fi
}

check_compose() {
  if ! has_cmd docker; then
    add_check "docker.command" fail missing_docker "install Docker before factory preflight" "{}"
    return
  fi
  if ! docker compose version >/dev/null 2>&1; then
    add_check "docker.compose" fail missing_compose "install docker compose v2 plugin" "{}"
    return
  fi
  add_check "docker.compose" pass ok "" "{}"

  local required_services=(
    postgres
    influxdb
    mosquitto
    edge-api
    edge-daq
    ai-softsensor
    ai-ctrl-optimizer
    license-service
    edge-monitor
  )
  local ps_json
  ps_json="$(compose ps --format json 2>/dev/null || true)"
  if [ -z "$ps_json" ]; then
    add_check "docker.containers" fail compose_ps_failed "docker compose ps --format json returned no data" "{}"
    return
  fi
  local result
  result="$(python3 - "${required_services[@]}" <<'PY'
import json
import subprocess
import sys

required = sys.argv[1:]
try:
    raw = subprocess.check_output(
        ["docker", "compose", "ps", "--format", "json"],
        text=True,
    ).strip()
except (OSError, subprocess.SubprocessError) as exc:
    print(json.dumps({"ok": False, "reason": "compose_ps_failed", "error": str(exc)}, ensure_ascii=False))
    raise SystemExit(0)

items = []
if raw.startswith("["):
    items = json.loads(raw)
else:
    for line in raw.splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))

by_service = {}
for item in items:
    service = item.get("Service") or item.get("Name") or ""
    if service:
        by_service[service] = item

missing = []
bad = []
summary = {}
for service in required:
    item = by_service.get(service)
    if not item:
        missing.append(service)
        continue
    state = str(item.get("State") or item.get("state") or "").lower()
    health = str(item.get("Health") or item.get("health") or "").lower()
    summary[service] = {"state": state, "health": health}
    if state != "running":
        bad.append({"service": service, "state": state, "health": health})
    elif health and health not in {"healthy", "running"}:
        bad.append({"service": service, "state": state, "health": health})

ok = not missing and not bad
print(json.dumps({
    "ok": ok,
    "reason": "ok" if ok else "container_not_ready",
    "missing": missing,
    "bad": bad,
    "summary": summary,
}, ensure_ascii=False, separators=(",", ":")))
PY
)"
  if python3 -c 'import json,sys; raise SystemExit(0 if json.loads(sys.argv[1]).get("ok") else 1)' "$result"; then
    add_check "docker.containers" pass ok "" "$result"
  elif [ "$ALLOW_DEGRADED" = true ] && python3 -c 'import json,sys; data=json.loads(sys.argv[1]); allowed={"edge-daq","ai-softsensor"}; allowed_health={"unhealthy","starting"}; bad=data.get("bad") or []; raise SystemExit(0 if bad and all(item.get("service") in allowed and item.get("state") == "running" and item.get("health") in allowed_health for item in bad) else 1)' "$result"; then
    add_check "docker.containers" degraded factory_degraded "edge-daq/ai-softsensor may be unhealthy without PLC/data source; run --strict for final acceptance" "$result"
  else
    add_check "docker.containers" fail container_not_ready "all core services must be running and healthy when health exists" "$result"
  fi
}

check_tailnet_identity() {
  local result
  if result="$(python3 common/tailscale_status.py \
      --expected-version "${TAILSCALE_EXPECTED_VERSION:-}" \
      --login-server "${TS_LOGIN_SERVER:-}" \
      --identity-file "$DEVICE_JSON" 2>/dev/null)"; then
    add_check "tailnet.identity" pass ok "" "$result"
  elif [ "$ALLOW_DEGRADED" = true ]; then
    add_check "tailnet.identity" degraded actual_tailnet_unavailable \
      "remote channel unavailable; local PLC/HMI/control remain independent" "$result"
  else
    add_check "tailnet.identity" fail actual_tailnet_unavailable \
      "restore the pinned client and original Device-bound node identity" "$result"
  fi
}

check_cloud_device_identity() {
  local detail
  local identity_path="${DEVICE_IDENTITY_PATH:-/var/lib/drycontroller/device.json}"
  detail="$(json_detail cloud_ops_base_url "${CLOUD_OPS_BASE_URL:-}" device_identity_path "$identity_path" device_identity_present "$([ -f "$identity_path" ] && printf true || printf false)")"
  if [ -n "${CLOUD_OPS_BASE_URL:-}" ] && [ -f "$identity_path" ]; then
    add_check "cloud.device_identity" pass configured "" "$detail"
  elif [ "$ALLOW_DEGRADED" = true ]; then
    add_check "cloud.device_identity" degraded missing_cloud_device_identity "accepted for local checks; required before remote operation" "$detail"
  else
    add_check "cloud.device_identity" fail missing_cloud_device_identity "CLOUD_OPS_BASE_URL and active per-device identity must be configured" "$detail"
  fi
}

should_check_ota() {
  if [ "$CHECK_OTA" = true ] || [ "$CHECK_OTA" = false ]; then
    printf '%s' "$CHECK_OTA"
    return
  fi
  if systemctl is-active --quiet drycontroller-ota-helper.service 2>/dev/null; then
    printf true
  else
    printf false
  fi
}

check_ota_helper() {
  if [ ! -S "$OTA_HELPER_SOCKET" ]; then
    add_check "ota_helper.ready" fail socket_missing "drycontroller-ota-helper.service must own the fixed UDS" "{}"
    return
  fi
  local body
  if body="$(curl -fsS --max-time "$TIMEOUT_SEC" --unix-socket "$OTA_HELPER_SOCKET" http://localhost/ready 2>/dev/null)"; then
    add_check "ota_helper.ready" pass ok "" "$body"
  else
    add_check "ota_helper.ready" fail unreachable "drycontroller-ota-helper.service must report ready=true" "{}"
  fi
}

check_file_exists "bootstrap.sentinel" "$SENTINEL" "bootstrap must finish before factory release"
check_device_identity
check_file_exists "license.file" "$LICENSE_FILE" "signed license must be installed"
check_file_exists "license.trust_policy" "$LICENSE_TRUST_POLICY" "license trust policy must be initialized"
check_license_verify
check_file_exists "cosign.pubkey" "$COSIGN_PUBKEY" "cosign public key must be installed for OTA image verification"
check_compose
http_check "edge_api.ready" "http://${EDGE_API_READY_HOST}:${EDGE_API_PORT}/ready" true
http_check "edge_daq.ready" "http://127.0.0.1:${EDGE_DAQ_HEALTH_PORT}/ready" true
http_check "edge_monitor.ready" "http://127.0.0.1:${EDGE_MONITOR_PORT}/ready" false
if [ "$(should_check_ota)" = true ]; then
  check_ota_helper
else
  add_check "ota_helper.ready" degraded skipped "root OTA helper is not active; pass --check-ota to require it" "{}"
fi
check_tailnet_identity
check_cloud_device_identity

python3 - "$CHECKS_FILE" "$ALLOW_DEGRADED" <<'PY'
import datetime as dt
import json
import socket
import sys
from collections import Counter

checks_path = sys.argv[1]
allow_degraded = sys.argv[2].lower() == "true"

checks = []
with open(checks_path, "r", encoding="utf-8") as fh:
    for line in fh:
        line = line.rstrip("\n")
        if not line:
            continue
        check_id, status, reason, hint, detail_raw = line.split("\t", 4)
        try:
            detail = json.loads(detail_raw)
        except json.JSONDecodeError:
            detail = {"raw": detail_raw}
        checks.append({
            "id": check_id,
            "status": status,
            "reason": reason,
            "hint": hint,
            "detail": detail,
        })

counts = Counter(c["status"] for c in checks)
has_fail = counts["fail"] > 0
has_degraded = counts["degraded"] > 0
success = not has_fail and (allow_degraded or not has_degraded)
status = "fail" if has_fail else ("degraded" if has_degraded else "pass")
if has_degraded and not allow_degraded:
    status = "fail"

payload = {
    "success": success,
    "status": status,
    "host": socket.gethostname(),
    "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "allow_degraded": allow_degraded,
    "checks": checks,
    "summary": {
        "pass": counts["pass"],
        "degraded": counts["degraded"],
        "fail": counts["fail"],
        "total": len(checks),
    },
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
raise SystemExit(0 if success else 1)
PY
