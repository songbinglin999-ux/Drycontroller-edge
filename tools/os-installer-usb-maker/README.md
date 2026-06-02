# OS Installer USB Maker

Operator-facing macOS UI for building the DryController OS Installer USB.

## Scope

- Fixed operator account: `dryops`.
- Fixed default password source: `infra/autoinstall/password.env`.
- Optional WiFi inputs: SSID, password, and interface.
- Factory activation material inputs: `tailnet.env` and `factory-license.json`.
- Device identity, production license, formal tailnet key, OTA permission, and later credentials are issued by Cloud Ops after first contact and operator approval.

This tool no longer creates a Provisioning USB. The old batch / installer-token
flow is legacy/emergency only and must not be used as the normal factory install
path.

The UI does not show or ask for the operator password. It passes the local, ignored password env file to `scripts/build-autoinstall-bundle.sh`, which injects only a sha512-crypt hash into `dist/autoinstall/user-data`.

Usable release images are named `OSYYYYMMDD-alpha.iso` or
`OSYYYYMMDD-beta.iso`. The UI defaults to the current date plus `alpha`;
change the suffix in the output ISO field when promoting a later build.

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
- `dist/bundle-images.tar` exists when the default "include business Docker images" option is enabled.
- `dist/autoinstall/tailnet.env` and
  `dist/autoinstall/factory-license.json` exist when the default "include
  factory activation materials" option is enabled. These are batch materials:
  rotate or replace them without rebuilding the edge source bundle or offline
  apt repo.
- Docker is available if host `xorriso` is not installed.

USB writing is destructive. The UI only lists external physical disks, but the operator must still verify the disk name, size, and device number before confirming.
