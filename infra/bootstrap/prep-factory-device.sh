#!/usr/bin/env bash
# Turn a vanilla Ubuntu Server 24.04 IPC into an unregistered, lane-ready
# factory Edge device (audit item FACTORY-DEVICE-PREP-20260903).
#
# The bundle carries no secrets: public cloud trust key, public line config,
# pinned tailscale deb, verified identity wheelhouse and the operator SSH key.
# Per-device random secrets are generated on this host by
# initialize-runtime-config.py. Identity/License/tailnet binding happens only
# later on the factory lane. Already-registered hosts are refused.
set -euo pipefail

readonly FROZEN_FACTORY_ADDRESS="169.254.203.2/30"
readonly FACTORY_BASE_URL="http://169.254.203.2:8765"
readonly RUNTIME_ROOT="/opt/drycontroller-identity"
readonly IDENTITY_STATE_DIR="/var/lib/drycontroller-identity"
readonly DEVICE_STATE="/var/lib/drycontroller/device.json"
readonly SSH_USER="dryops"
readonly SSHD_DROPIN="/etc/ssh/sshd_config.d/60-drycontroller-prep.conf"
readonly SELF_CHECK_TIMEOUT_SECONDS="60"

BUNDLE=""
SHUTDOWN="no"

fail() {
  printf '[factory-device-prep] %s\n' "$1" >&2
  exit "${2:-1}"
}

usage() {
  cat <<'EOF'
Usage:
  sudo prep-factory-device.sh --bundle DIR [--shutdown]

Bundle layout (produced by scripts/prep-device-from-mac.sh):
  repo/                           exact edge repo source tree
  wheelhouse/                     wheels + SHA256SUMS
  wheelhouse.manifest.sha256      approved sha256 of wheelhouse/SHA256SUMS
  tailscale/tailscale.deb         pinned client package
  tailscale/tailscale-client.lock.json
  public-config.json              public per-line values (no secrets)
  device-identity-cloud.pub       b64(raw32) cloud identity public key
  device-identity-cloud.pub.sha256
  ssh_authorized_keys             operator keys to install for dryops
  env-template                    env.example placeholder template
EOF
}

required_value() {
  [ -n "${2:-}" ] || fail "$1 requires a value" 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --bundle) required_value "$1" "${2:-}"; BUNDLE="$2"; shift 2 ;;
    --shutdown) SHUTDOWN="yes"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; fail "unknown argument: $1" 2 ;;
  esac
done

[ -n "$BUNDLE" ] || { usage >&2; fail "--bundle is required" 2; }
BUNDLE="$(cd "$BUNDLE" && pwd -P)"

step() {
  printf '[factory-device-prep] %s\n' "$1"
}

python3 - <<'PY'
import sys
if sys.version_info[:2] < (3, 12):
    raise SystemExit("python3 >= 3.12 is required")
PY

step "preflight"
[ "$(id -u)" -eq 0 ] || fail "prep must run as root" 2
[ "$(uname -s)" = "Linux" ] && [ "$(uname -m)" = "x86_64" ] \
  || fail "Linux amd64 is required" 2
grep -q '^VERSION_ID="24\.04"' /etc/os-release \
  || fail "Ubuntu Server 24.04 is required" 2
[ -c /dev/tpmrm0 ] || fail "TPM 2.0 resource manager /dev/tpmrm0 is required" 3
command -v systemctl >/dev/null 2>&1 || fail "systemd is required" 2
command -v netplan >/dev/null 2>&1 || fail "netplan is required" 2
if [ -e "${IDENTITY_STATE_DIR}/factory-closed.json" ] || [ -e "$DEVICE_STATE" ]; then
  fail "device already carries activation state; prep is only for unregistered devices (human decision required)" 3
fi
for path in \
  "$BUNDLE/repo/infra/bootstrap/initialize-runtime-config.py" \
  "$BUNDLE/repo/infra/bootstrap/install-device-identity-runtime.py" \
  "$BUNDLE/repo/infra/bootstrap/drycontroller-factory-identity.service" \
  "$BUNDLE/repo/env.example" \
  "$BUNDLE/wheelhouse/SHA256SUMS" \
  "$BUNDLE/wheelhouse.manifest.sha256" \
  "$BUNDLE/tailscale/tailscale.deb" \
  "$BUNDLE/tailscale/tailscale-client.lock.json" \
  "$BUNDLE/public-config.json" \
  "$BUNDLE/device-identity-cloud.pub" \
  "$BUNDLE/device-identity-cloud.pub.sha256" \
  "$BUNDLE/ssh_authorized_keys" \
  "$BUNDLE/env-template"; do
  [ -f "$path" ] && [ ! -L "$path" ] || fail "bundle input missing: $path" 2
done
cmp -s "$BUNDLE/env-template" "$BUNDLE/repo/env.example" \
  || fail "env template does not match the repo template" 2

step "verify bundle integrity"
BUNDLE_CHECK="$(python3 - "$BUNDLE" <<'PY'
import base64
import hashlib
import json
import re
import sys
from pathlib import Path

bundle = Path(sys.argv[1])
errors = []

lock = json.loads((bundle / "tailscale/tailscale-client.lock.json").read_text(encoding="utf-8"))
deb = bundle / "tailscale/tailscale.deb"
if hashlib.sha256(deb.read_bytes()).hexdigest() != lock["deb_sha256"]:
    errors.append("tailscale deb checksum mismatch")

public = (bundle / "device-identity-cloud.pub").read_text(encoding="ascii").strip()
raw = base64.b64decode(public, validate=True)
if len(raw) != 32:
    errors.append("cloud identity public key is not a raw 32-byte ed25519 key")
expected_sha = (bundle / "device-identity-cloud.pub.sha256").read_text(encoding="ascii").strip()
if hashlib.sha256(raw).hexdigest() != expected_sha:
    errors.append("cloud identity public key checksum mismatch")

manifest_sha = (bundle / "wheelhouse.manifest.sha256").read_text(encoding="ascii").strip()
sums = (bundle / "wheelhouse/SHA256SUMS").read_bytes()
if hashlib.sha256(sums).hexdigest() != manifest_sha:
    errors.append("wheelhouse manifest checksum mismatch")

config = json.loads((bundle / "public-config.json").read_text(encoding="utf-8"))
if config.get("factory_address") != "169.254.203.2/30":
    errors.append("public config factory address is not the frozen lane channel")
if not re.fullmatch(r"(?:en|eth)[A-Za-z0-9_.-]{2,14}", config.get("factory_interface", "")):
    errors.append("public config factory interface invalid")

keys = (bundle / "ssh_authorized_keys").read_text(encoding="utf-8").splitlines()
key_lines = [line for line in keys if line.strip() and not line.lstrip().startswith("#")]
if not key_lines or any(
    not re.match(r"^(?:ssh-rsa|ssh-ed25519|ecdsa-sha2-[a-z0-9-]+|sk-(?:ssh-ed25519|ecdsa-sha2-[a-z0-9-]+))\s", line)
    for line in key_lines
):
    errors.append("ssh_authorized_keys must contain only public key lines")
for line in key_lines:
    if any(character in line for character in ";&|$<>" + chr(96)):
        errors.append("ssh_authorized_keys contains shell metacharacters")

print("\n".join(errors))
PY
)" || fail "bundle verification script failed" 2
[ -z "$BUNDLE_CHECK" ] || fail "bundle verification failed:\n$BUNDLE_CHECK" 2

step "install host prerequisites"
dpkg -s python3.12-venv >/dev/null 2>&1 || {
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.12-venv >/dev/null \
    || { apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.12-venv >/dev/null; }
}

step "install pinned tailscale client (no join)"
LOCK_VERSION="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$BUNDLE/tailscale/tailscale-client.lock.json")"
dpkg -i "$BUNDLE/tailscale/tailscale.deb" >/dev/null
INSTALLED_VERSION="$(tailscale version | head -n1 | awk '{print $1}')"
[ "$INSTALLED_VERSION" = "$LOCK_VERSION" ] \
  || fail "installed tailscale $INSTALLED_VERSION does not match the pinned lock $LOCK_VERSION" 2
systemctl enable --now tailscaled >/dev/null 2>&1 || true

step "install cloud identity trust public key"
install -d -o root -g root -m 0755 /etc/drycontroller/trust
install -o root -g root -m 0644 \
  "$BUNDLE/device-identity-cloud.pub" /etc/drycontroller/trust/device-identity-cloud.pub

step "initialize runtime config (per-device secrets generated locally)"
python3 "$BUNDLE/repo/infra/bootstrap/initialize-runtime-config.py" \
  --public-config "$BUNDLE/public-config.json" \
  --tailscale-client-lock "$BUNDLE/tailscale/tailscale-client.lock.json" \
  --env-template "$BUNDLE/env-template" \
  || fail "runtime configuration failed" 4
netplan apply

step "install device identity runtime"
if [ -d "$RUNTIME_ROOT" ] && [ ! -L "$RUNTIME_ROOT" ]; then
  PYTHONPATH="$RUNTIME_ROOT" "$RUNTIME_ROOT/venv/bin/python" \
    -c 'from identity_service.backend import TpmIdentityBackend' 2>/dev/null \
    || fail "existing identity runtime is unhealthy; human decision required" 3
  step "identity runtime already installed and healthy; skipping installer"
else
  python3 "$BUNDLE/repo/infra/bootstrap/install-device-identity-runtime.py" \
    --repo-source "$BUNDLE/repo" \
    --wheelhouse "$BUNDLE/wheelhouse" \
    --approved-wheelhouse-manifest-sha256 "$(cat "$BUNDLE/wheelhouse.manifest.sha256")" \
    || fail "device identity runtime installation failed" 4
fi

step "enable factory identity endpoint (lane ${FROZEN_FACTORY_ADDRESS})"
install -o root -g root -m 0644 \
  "$BUNDLE/repo/infra/bootstrap/drycontroller-factory-identity.service" \
  /etc/systemd/system/drycontroller-factory-identity.service
systemctl daemon-reload
systemctl enable --now drycontroller-factory-identity.service >/dev/null

step "harden ssh (key-only for ${SSH_USER}; console rescue stays)"
id "$SSH_USER" >/dev/null 2>&1 || fail "user ${SSH_USER} is missing; install convention requires it" 2
install -d -o "$SSH_USER" -g "$SSH_USER" -m 0700 "/home/${SSH_USER}/.ssh"
install -o "$SSH_USER" -g "$SSH_USER" -m 0600 \
  "$BUNDLE/ssh_authorized_keys" "/home/${SSH_USER}/.ssh/authorized_keys"
install -d -o root -g root -m 0755 /etc/ssh/sshd_config.d
printf 'PasswordAuthentication no\n' > "$SSHD_DROPIN"
sshd -t || fail "sshd configuration rejected; not restarting ssh" 5
systemctl restart ssh

step "self-check factory endpoint"
SELF_CHECK="$(python3 - "$SELF_CHECK_TIMEOUT_SECONDS" <<'PY'
import json
import sys
import time
import urllib.request

deadline = time.monotonic() + int(sys.argv[1])
last_error = "self-check did not run"
while time.monotonic() < deadline:
    try:
        health = json.load(urllib.request.urlopen(f"http://169.254.203.2:8765/health", timeout=5))
        identity = json.load(urllib.request.urlopen(f"http://169.254.203.2:8765/factory-identity", timeout=5))
        if health.get("status") != "ok":
            last_error = "factory identity health is not ok"
        elif not isinstance(identity.get("identity_fingerprint"), str) or len(identity["identity_fingerprint"]) != 64:
            last_error = "factory identity payload is invalid"
        else:
            print(json.dumps({"identity_fingerprint": identity["identity_fingerprint"]}))
            sys.exit(0)
    except Exception as exc:  # noqa: BLE001 - retry until the lane address is up
        last_error = str(exc)
    time.sleep(2)
raise SystemExit(f"factory endpoint self-check failed: {last_error}")
PY
)" || fail "factory endpoint self-check failed" 5

systemctl is-active --quiet drycontroller-factory-identity.service \
  || fail "factory identity service is not active" 5

printf '[factory-device-prep] PASS %s\n' "$SELF_CHECK"
printf '[factory-device-prep] device is lane-ready: put it on the factory lane and power it on for the station watch.\n'

if [ "$SHUTDOWN" = "yes" ]; then
  step "shutting down"
  systemctl poweroff
fi
