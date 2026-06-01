#!/bin/bash
#
# Install DryController tailnet and activation systemd services.
#
# Usage: sudo ./install-tailnet-services.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INFRA_DIR="${SCRIPT_DIR}"

echo "Installing DryController tailnet services..."

# Create required directories
mkdir -p /var/lib/drycontroller
mkdir -p /var/lib/tailscale
mkdir -p /etc/drycontroller

# Copy systemd unit files
cp "${INFRA_DIR}/drycontroller-tailnet.service" /etc/systemd/system/
cp "${INFRA_DIR}/drycontroller-activation.service" /etc/systemd/system/

# Reload systemd
systemctl daemon-reload

# Enable services
systemctl enable drycontroller-tailnet.service
systemctl enable drycontroller-activation.service

echo "Tailnet services installed."
echo ""
echo "To start now:"
echo "  systemctl start drycontroller-tailnet"
echo "  systemctl start drycontroller-activation"
echo ""
echo "To check status:"
echo "  systemctl status drycontroller-tailnet"
echo "  systemctl status drycontroller-activation"
