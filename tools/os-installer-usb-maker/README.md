# OS Installer USB Maker

Operator-facing macOS UI for building the DryController OS Installer USB.

The UI is intentionally built for non-developer operators:

1. Check the "准备状态" panel.
2. Select or confirm the batch materials.
3. Confirm the target USB disk name and size.
4. Click the primary button to build and write the installer.

Fields that normally do not need changes keep safe defaults. WiFi stays off by
default and is only enabled when the install area has no wired network. When
WiFi is enabled, the SSID field can scan this Mac's visible WiFi networks for a
drop-down selection, while still allowing manual entry for hidden or off-site
factory networks.

## Scope

- Fixed operator account: `dryops`.
- Fixed default password source: `infra/autoinstall/password.env`.
- Optional WiFi inputs: SSID, password, and interface.
- Factory activation material input: `activation-bundle.json`.
- Device identity, production license, node enroll token, OTA permission, and later credentials are issued by Cloud Ops after first valid factory activation contact; the current tailnet node is bound during that activation. Manual operator handling is only for legacy pending or exceptional records.
- The primary action is disabled until required local materials and the target
  disk are present.

This tool no longer creates a Provisioning USB. The old batch / installer-token
flow is retired and must not be used as the normal factory install path.

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
- `dist/autoinstall/activation-bundle.json` exists when the default "include
  factory activation materials" option is enabled. This is batch material
  prepared and handed off by Cloud Ops / factory administrators, not generated
  by field operators. Put it in `dist/autoinstall/`, then click "重新检查" in the
  UI. Rotate or replace it without rebuilding the edge source bundle or offline
  apt repo.
- Docker is available if host `xorriso` is not installed.

USB writing is destructive. The UI only lists external physical disks, but the operator must still verify the disk name, size, and device number before confirming.
