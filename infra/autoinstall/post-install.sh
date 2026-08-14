#!/usr/bin/env bash
#
# Runs inside the installed Ubuntu target during autoinstall late-commands.
# It unpacks the public installer source and delegates the signed H1/T1/S1
# transaction to the only host installer entrypoint.
#
set -euo pipefail

LOG_FILE="/var/log/drycontroller-autoinstall.log"
BUNDLE="/opt/drycontroller-edge.tar.gz"
SOFTWARE_IMAGE_BUNDLE="/opt/drycontroller-release/software-images.tar"
HOST_IMAGE_BUNDLE="/opt/drycontroller-release/host-image.tar"
SOFTWARE_RELEASE="/opt/drycontroller-release/software-release-seed.json"
HOST_RELEASE="/opt/drycontroller-release/host-release-seed.json"
SOFTWARE_COSIGN_PUBLIC_KEY="/opt/drycontroller-release/software-release-cosign.pub"
HOST_COSIGN_PUBLIC_KEY="/opt/drycontroller-release/host-release-cosign.pub"
COSIGN_BIN="/opt/drycontroller-release/cosign-linux-amd64"
DEVICE_IDENTITY_CLOUD_PUBLIC_KEY="/opt/drycontroller-release/device-identity-cloud.pub"
IDENTITY_WHEELHOUSE="/opt/drycontroller-release/identity-wheelhouse"
PUBLIC_CONFIG="/opt/drycontroller-release/public-config.json"
SOURCE_STAGE="/opt/drycontroller-edge-source"
SOURCE_ROOT="$SOURCE_STAGE"

log() {
  printf '[%s] %s\n' "$(date -Is)" "$*" | tee -a "$LOG_FILE" >/dev/null
}

die() {
  log "ERROR: $*"
  exit 1
}

[ "$(id -u)" -eq 0 ] || die "post-install must run as root"
[ -f "$BUNDLE" ] || die "missing edge bundle: $BUNDLE"
[ -f "$SOFTWARE_IMAGE_BUNDLE" ] || die "missing complete initial SoftwareRelease image bundle"
[ -f "$HOST_IMAGE_BUNDLE" ] || die "missing initial HostRelease image bundle"
[ -f "$SOFTWARE_RELEASE" ] || die "missing signed initial SoftwareRelease"
[ -f "$HOST_RELEASE" ] || die "missing signed initial HostRelease"
[ -f "$SOFTWARE_COSIGN_PUBLIC_KEY" ] || die "missing SoftwareRelease trust root"
[ -f "$HOST_COSIGN_PUBLIC_KEY" ] || die "missing HostRelease trust root"
[ -f "$COSIGN_BIN" ] || die "missing Linux amd64 cosign binary: $COSIGN_BIN"
[ -f "$DEVICE_IDENTITY_CLOUD_PUBLIC_KEY" ] || die "missing Cloud device identity trust root"
[ -d "$IDENTITY_WHEELHOUSE" ] || die "missing offline device identity wheelhouse"
[ -f "$PUBLIC_CONFIG" ] || die "missing public OS Installer configuration"

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
  --host-release "$HOST_RELEASE" \
  --host-image-bundle "$HOST_IMAGE_BUNDLE" \
  --software-release "$SOFTWARE_RELEASE" \
  --software-image-bundle "$SOFTWARE_IMAGE_BUNDLE" \
  --host-cosign-public-key "$HOST_COSIGN_PUBLIC_KEY" \
  --software-cosign-public-key "$SOFTWARE_COSIGN_PUBLIC_KEY" \
  --cosign-bin "$COSIGN_BIN" \
  --device-identity-cloud-key "$DEVICE_IDENTITY_CLOUD_PUBLIC_KEY" \
  --identity-wheelhouse "$IDENTITY_WHEELHOUSE" \
  --public-config "$PUBLIC_CONFIG"

rm -rf /opt/drycontroller-release "$SOURCE_STAGE"
rm -f "$BUNDLE"
log "signed initial H1/T1/S1 installed"

systemctl disable drycontroller-edge-post-install.service >/dev/null 2>&1 || true

log "autoinstall host preparation complete"
log "shutting down. Remove OS Installer USB, connect the isolated factory link, then power on."
umount -l /cdrom 2>/dev/null || true
sync
sleep 1
/sbin/poweroff -f
