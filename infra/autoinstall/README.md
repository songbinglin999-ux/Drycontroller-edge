# DryController Edge Ubuntu Autoinstall

This directory contains the unattended Ubuntu Server installer profile for a
DryController edge host.

The installer USB has one job: turn a blank IPC into a standard Linux host with
Docker, the edge repository, and `drycontroller-bootstrap.service` enabled.
Device identity and secrets are not placed in this installer profile; they are
provided later by the provisioning USB.

## Files

- `user-data`: Ubuntu autoinstall profile.
- `meta-data`: cloud-init datasource metadata.
- `post-install.sh`: runs inside the installed target and calls
  `infra/bootstrap/install-edge-host.sh`.
- `drycontroller-edge-post-install.service`: first-boot systemd unit that runs
  `post-install.sh` after the installed OS boots for real.
- `wifi.env.example`: template for factory WiFi credentials. Copy to
  `wifi.env` on the installer media when wired Ethernet is not available.

## Required USB Root Files

Place these files at the root of the Ubuntu installer media:

```text
user-data
meta-data
post-install.sh
drycontroller-edge-post-install.service
wifi.env              # optional, copied from wifi.env.example and filled by operator/team
drycontroller-edge.tar.gz
```

`drycontroller-edge.tar.gz` must contain the edge repository. The tar can contain
files directly at the root, or under one top-level `Drycontroller-edge/`
directory.

## Boot Parameters

Ubuntu Server autoinstall needs the installer kernel command line to include:

```text
autoinstall ds=nocloud;s=/cdrom/
```

Most ISO remastering tools can add this to the GRUB menu entry. For a first
factory validation, it is acceptable to edit the GRUB command line manually once
at boot and then bake the final media after the flow is confirmed.
