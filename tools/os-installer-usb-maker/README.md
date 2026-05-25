# OS Installer USB Maker

Operator-facing macOS UI for building the DryController OS Installer USB.

## Scope

- Fixed operator account: `dryops`.
- Fixed default password source: `infra/autoinstall/password.env`.
- Optional WiFi inputs: SSID, password, and interface.
- Device identity, license, registry credentials, and Headscale settings still belong to the Provisioning USB flow.

The UI does not show or ask for the operator password. It passes the local, ignored password env file to `scripts/build-autoinstall-bundle.sh`, which injects only a sha512-crypt hash into `dist/autoinstall/user-data`.

## Run

```bash
open tools/os-installer-usb-maker/launch.command
```

or:

```bash
/usr/bin/python3 tools/os-installer-usb-maker/os_installer_usb_maker.py
```

## Build macOS App

```bash
bash tools/os-installer-usb-maker/build_app.sh
```

This regenerates `DryController USB Maker.app` and `icon.icns`. They are local
build artifacts and should not be committed.

## Preconditions

- `infra/autoinstall/password.env` exists on the maker Mac.
- `dist/drycontroller-os-installer-hybrid.iso` exists as the known bootable base ISO.
- `dist/autoinstall/drycontroller-apt/Packages` exists, or the UI option to rebuild the offline apt repo is enabled.
- Docker is available if host `xorriso` is not installed.

USB writing is destructive. The UI only lists external physical disks, but the operator must still verify the disk name, size, and device number before confirming.
