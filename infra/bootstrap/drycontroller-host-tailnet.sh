#!/usr/bin/env bash
#
# Join the host OS to the Drycontroller tailnet.
#
# This script intentionally runs outside Docker.  The tailnet is the survival
# access path and must not depend on docker compose being up.
#
set -euo pipefail

ENV_FILE="${DRYCONTROLLER_TAILNET_ENV:-/etc/drycontroller/tailnet.env}"
BUNDLE_FILE="${DRYCONTROLLER_ACTIVATION_BUNDLE:-/etc/drycontroller/activation-bundle.json}"
LOG_PREFIX="[drycontroller-tailnet]"

log() {
  printf '%s %s\n' "$LOG_PREFIX" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

materialize_activation_bundle() {
  [ -f "$BUNDLE_FILE" ] || return 0

  BUNDLE_FILE="$BUNDLE_FILE" ENV_FILE="$ENV_FILE" python3 - <<'PY'
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

path = Path(os.environ["ENV_FILE"])
path.parent.mkdir(parents=True, exist_ok=True)
lines = [f"{key}={shlex.quote(value)}" for key, value in sorted(values.items())]
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
  chmod 0600 "$ENV_FILE" || true
  log "materialized tailnet env from activation bundle"
}

[ "$(id -u)" -eq 0 ] || die "run as root"
if [ ! -f "$ENV_FILE" ] && [ ! -f "$BUNDLE_FILE" ]; then
  log "no tailnet env or activation bundle; skipping tailnet join"
  exit 0
fi

command -v python3 >/dev/null 2>&1 || die "python3 is not installed"
materialize_activation_bundle
[ -f "$ENV_FILE" ] || die "missing tailnet env: $ENV_FILE"

# shellcheck disable=SC1090
. "$ENV_FILE"

[ -n "${TS_LOGIN_SERVER:-}" ] || die "TS_LOGIN_SERVER is required"
[ -n "${TS_HOSTNAME:-}" ] || die "TS_HOSTNAME is required"

command -v systemctl >/dev/null 2>&1 || die "systemd is required"
command -v tailscale >/dev/null 2>&1 || die "tailscale CLI is not installed"

if [ -f /etc/drycontroller/keys/headscale-ca.crt ] && [ -d /usr/local/share/ca-certificates ]; then
  cp /etc/drycontroller/keys/headscale-ca.crt /usr/local/share/ca-certificates/headscale-ca.crt
  update-ca-certificates >/dev/null 2>&1 || true
fi

if systemctl list-unit-files tailscaled.service 2>/dev/null | grep -q '^tailscaled.service'; then
  systemctl enable tailscaled.service >/dev/null 2>&1 || true
  systemctl start tailscaled.service
fi

existing_ip="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
if [ -n "$existing_ip" ]; then
  log "tailnet already has IPv4: ${existing_ip}"
  exit 0
fi

[ -n "${TS_AUTHKEY:-}" ] || die "TS_AUTHKEY is required when no existing tailnet IPv4 is available"

args=(
  up
  --authkey="${TS_AUTHKEY}"
  --login-server="${TS_LOGIN_SERVER}"
  --hostname="${TS_HOSTNAME}"
)

if [ -n "${TS_EXTRA_ARGS:-}" ]; then
  # TS_EXTRA_ARGS is an operator escape hatch. Keep login-server/hostname out of
  # it in generated config so the required flags stay single-sourced above.
  # shellcheck disable=SC2206
  extra_args=(${TS_EXTRA_ARGS})
  args+=("${extra_args[@]}")
fi

log "joining tailnet as ${TS_HOSTNAME} via ${TS_LOGIN_SERVER}"
tailscale "${args[@]}"

tailnet_ip="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
[ -n "$tailnet_ip" ] || die "tailscale joined but no IPv4 address is available"
log "tailnet IPv4: ${tailnet_ip}"
