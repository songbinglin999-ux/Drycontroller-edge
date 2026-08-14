# OS Installer USB Maker

`os_installer_usb_maker.py` is a non-interactive helper used only by the RB-6
release wrapper. It remasters the controlled Ubuntu base ISO and verifies the
public-only media contract, exact H/S pair, both manifest signatures, archive
contents, offline dependencies, and boot configuration.

It is not a supported command or GUI and cannot write disks. Build only through
`scripts/build-os-installer-release.sh`; write only through
`scripts/write-os-installer-usb.sh`. The workspace audit gates still apply.
