#!/usr/bin/env bash
#
# Join the host OS to the Drycontroller tailnet.
#
# This script intentionally runs outside Docker.  The tailnet is the survival
# access path and must not depend on docker compose being up.
#
set -euo pipefail

ENV_FILE="${DRYCONTROLLER_TAILNET_ENV:-/etc/drycontroller/tailnet.env}"
LOG_PREFIX="[drycontroller-tailnet]"

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
