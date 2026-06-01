#!/usr/bin/env bash
#
# Report the host tailnet address and edge status to cloud-ops.
#
# This replaces the old docker-compose edge-enroll container.  It runs on the
# host so cloud visibility survives docker compose down/up failures.
#
set -euo pipefail

ENV_FILE="${DRYCONTROLLER_TAILNET_ENV:-/etc/drycontroller/tailnet.env}"
LOG_PREFIX="[drycontroller-enroll]"

log() {
  printf '%s %s\n' "$LOG_PREFIX" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

[ -f "$ENV_FILE" ] || die "missing tailnet env: $ENV_FILE"

# shellcheck disable=SC1090
. "$ENV_FILE"

[ -n "${CLOUD_OPS_BASE_URL:-}" ] || die "CLOUD_OPS_BASE_URL is required"
[ -n "${CLOUD_OPS_ENROLL_TOKEN:-}" ] || die "CLOUD_OPS_ENROLL_TOKEN is required"
[ -n "${ENROLL_NAME:-}" ] || die "ENROLL_NAME is required"

command -v tailscale >/dev/null 2>&1 || die "tailscale CLI is not installed"
command -v curl >/dev/null 2>&1 || die "curl is not installed"
command -v python3 >/dev/null 2>&1 || die "python3 is not installed"

CLOUD_OPS_ENROLL_TOKEN_HEADER="${CLOUD_OPS_ENROLL_TOKEN_HEADER:-X-Enroll-Token}"
ENROLL_EDGE_API_PORT="${ENROLL_EDGE_API_PORT:-8000}"
ENROLL_INTERVAL_SEC="${ENROLL_INTERVAL_SEC:-60}"
LINE_ID="${LINE_ID:-L1}"

json_payload() {
  python3 - "$@" <<'PY'
import json
import os
import sys

mode = sys.argv[1]
if mode == "enroll":
    payload = {
        "name": os.environ["ENROLL_NAME"],
        "address": os.environ["TAILNET_IP"],
        "edge_api_port": int(os.environ["ENROLL_EDGE_API_PORT"]),
    }
elif mode == "status":
    payload = {
        "name": os.environ["ENROLL_NAME"],
        "address": os.environ["TAILNET_IP"],
        "edge_api_port": int(os.environ["ENROLL_EDGE_API_PORT"]),
        "line_id": os.environ.get("LINE_ID", "L1"),
        "system_status_b64": os.environ["STATUS_B64"],
    }
else:
    raise SystemExit(f"unknown payload mode: {mode}")
print(json.dumps(payload, ensure_ascii=False))
PY
}

while true; do
  TAILNET_IP="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
  export TAILNET_IP ENROLL_EDGE_API_PORT LINE_ID

  if [ -z "$TAILNET_IP" ]; then
    log "no tailnet IPv4 yet; retry"
    sleep 3
    continue
  fi

  enroll_url="${CLOUD_OPS_BASE_URL%/}/api/v1/enroll"
  log "enrolling name=${ENROLL_NAME} address=${TAILNET_IP} port=${ENROLL_EDGE_API_PORT} -> ${enroll_url}"
  if curl -fsS \
    -H "Content-Type: application/json" \
    -H "${CLOUD_OPS_ENROLL_TOKEN_HEADER}: ${CLOUD_OPS_ENROLL_TOKEN}" \
    -d "$(json_payload enroll)" \
    "$enroll_url" >/dev/null; then
    log "enroll ok"
  else
    log "enroll failed"
  fi

  status_url="http://${TAILNET_IP}:${ENROLL_EDGE_API_PORT}/api/v1/system/status?line=${LINE_ID}"
  status_json="$(curl -fsS --max-time 4 "$status_url" 2>/dev/null || true)"
  if [ -n "$status_json" ]; then
    STATUS_B64="$(printf '%s' "$status_json" | base64 | tr -d '\n')"
    export STATUS_B64
    report_url="${CLOUD_OPS_BASE_URL%/}/api/v1/edge-status"
    log "reporting status -> ${report_url} (b64_bytes=$(printf '%s' "$STATUS_B64" | wc -c))"
    if curl -fsS \
      -H "Content-Type: application/json" \
      -H "${CLOUD_OPS_ENROLL_TOKEN_HEADER}: ${CLOUD_OPS_ENROLL_TOKEN}" \
      -d "$(json_payload status)" \
      "$report_url" >/dev/null; then
      log "status report ok"
    else
      log "status report failed"
    fi
  else
    log "system/status unavailable yet; skip report"
  fi

  sleep "$ENROLL_INTERVAL_SEC"
done
