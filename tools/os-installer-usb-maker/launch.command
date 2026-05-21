#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
exec /usr/bin/python3 tools/os-installer-usb-maker/os_installer_usb_maker.py
