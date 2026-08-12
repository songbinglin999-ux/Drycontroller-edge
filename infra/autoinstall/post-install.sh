#!/usr/bin/env bash
#
# Runs inside the installed Ubuntu target during autoinstall late-commands.
# It unpacks the bundled edge repository and delegates host preparation to the
# normal bootstrap installer so manual and autoinstall paths stay aligned.
#
set -euo pipefail

LOG_FILE="/var/log/drycontroller-autoinstall.log"
BUNDLE="/opt/drycontroller-edge.tar.gz"
IMAGE_BUNDLE="/opt/drycontroller-images/bundle-images.tar"
INITIAL_RELEASE="/opt/drycontroller-release/software-release-seed.json"
COSIGN_PUBLIC_KEY="/opt/drycontroller-release/cosign.pub"
COSIGN_BIN="/opt/drycontroller-release/cosign-linux-amd64"
SOURCE_ROOT="/opt/drycontroller-edge-source"
WIFI_ENV="/opt/drycontroller-wifi.env"

log() {
  printf '[%s] %s\n' "$(date -Is)" "$*" | tee -a "$LOG_FILE" >/dev/null
}

die() {
  log "ERROR: $*"
  exit 1
}

[ "$(id -u)" -eq 0 ] || die "post-install must run as root"
[ -f "$BUNDLE" ] || die "missing edge bundle: $BUNDLE"
[ -f "$IMAGE_BUNDLE" ] || die "missing complete initial image bundle: $IMAGE_BUNDLE"
[ -f "$INITIAL_RELEASE" ] || die "missing signed initial SoftwareRelease: $INITIAL_RELEASE"
[ -f "$COSIGN_PUBLIC_KEY" ] || die "missing initial release cosign public key: $COSIGN_PUBLIC_KEY"
[ -f "$COSIGN_BIN" ] || die "missing Linux amd64 cosign binary: $COSIGN_BIN"

configure_wifi() {
  [ -f "$WIFI_ENV" ] || return 0

  # shellcheck disable=SC1090
  . "$WIFI_ENV"
  [ -n "${WIFI_SSID:-}" ] || die "WIFI_SSID missing in $WIFI_ENV"
  [ -n "${WIFI_PASSWORD:-}" ] || die "WIFI_PASSWORD missing in $WIFI_ENV"

  log "configuring wifi connection for SSID: ${WIFI_SSID}"
  export DEBIAN_FRONTEND=noninteractive
  if ! command -v nmcli >/dev/null 2>&1; then
    apt-get update
    apt-get install -y --no-install-recommends network-manager
  fi

  systemctl enable NetworkManager.service >/dev/null 2>&1 || true
  systemctl restart NetworkManager.service

  if [ -n "${WIFI_INTERFACE:-}" ]; then
    nmcli device set "$WIFI_INTERFACE" managed yes || true
    nmcli device wifi connect "$WIFI_SSID" password "$WIFI_PASSWORD" ifname "$WIFI_INTERFACE"
  else
    nmcli device wifi connect "$WIFI_SSID" password "$WIFI_PASSWORD"
  fi

  log "wifi connection configured"
}

configure_wifi

log "unpacking edge bundle"
rm -rf "$SOURCE_ROOT"
mkdir -p "$SOURCE_ROOT"
tar -xzf "$BUNDLE" -C "$SOURCE_ROOT"

if [ -d "${SOURCE_ROOT}/Drycontroller-edge" ]; then
  SOURCE_ROOT="${SOURCE_ROOT}/Drycontroller-edge"
fi

[ -f "${SOURCE_ROOT}/infra/bootstrap/install-edge-host.sh" ] || die "install-edge-host.sh missing in bundle"

log "running install-edge-host.sh"
bash "${SOURCE_ROOT}/infra/bootstrap/install-edge-host.sh" \
  --source "$SOURCE_ROOT" \
  --initial-release "$INITIAL_RELEASE" \
  --image-bundle "$IMAGE_BUNDLE" \
  --cosign-public-key "$COSIGN_PUBLIC_KEY" \
  --cosign-bin "$COSIGN_BIN"

rm -f "$IMAGE_BUNDLE" "$INITIAL_RELEASE" "$COSIGN_PUBLIC_KEY" "$COSIGN_BIN"
log "signed initial active release installed"

systemctl disable drycontroller-edge-post-install.service >/dev/null 2>&1 || true

log "autoinstall host preparation complete"
log "shutting down. Remove OS Installer USB, then power on with network available."
log "after boot, host tailnet and factory activation services will run if activation-bundle.json is installed"
umount -l /cdrom 2>/dev/null || true
sync
sleep 1
/sbin/poweroff -f
