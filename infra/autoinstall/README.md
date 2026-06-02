# DryController Edge OS Installer USB

This directory contains the unattended Ubuntu Server installer profile for a
DryController edge host.

The **OS Installer USB** has one job: turn a blank IPC into a standard Linux
host with Docker, the edge repository, host Tailscale, and
`drycontroller-activation.service` enabled. Device identity and production
authorization are issued later by Cloud Ops after the host joins the bootstrap
tailnet.

This is destructive installation media. The autoinstall profile selects the
largest non-installer disk, wipes existing Windows/Linux partition metadata, and
recreates the DryController host partitions.

Terminology:

- **OS Installer USB**: Ubuntu autoinstall media maintained in the edge repo.
- **Provisioning USB**: legacy/emergency deployment media. It is no longer part
  of the normal factory activation path.

## Files

- `user-data`: Ubuntu autoinstall profile.
- `meta-data`: cloud-init datasource metadata.
- `post-install.sh`: runs inside the installed target and calls
  `infra/bootstrap/install-edge-host.sh`.
- `drycontroller-edge-post-install.service`: first-boot systemd unit that runs
  `post-install.sh` after the installed OS boots for real.
- `wifi.env.example`: template for factory WiFi credentials. Copy to
  `wifi.env` on the installer media when wired Ethernet is not available.
- `password.env.example`: template for the local, untracked password input used
  while building media. Copy to `password.env`; do not commit it or copy it to
  the installer media.
- `drycontroller-apt/`: generated offline apt repository copied to the installer
  media root. It contains Docker runtime, Docker Compose v2, NetworkManager, and
  host package dependencies so factory/site installs do not depend on public
  mirror reachability.
- `bundle-images.tar`: optional first-boot Docker image bundle copied from
  `dist/bundle-images.tar`. Include it for clean-room installs that must bring
  up compose without registry access.
- `tailnet.env`: factory activation batch material copied into
  `/etc/drycontroller/tailnet.env` by late commands.
- `factory-license.json`: signed factory license copied into
  `/etc/drycontroller/license/license.json` and archived under
  `/var/lib/drycontroller/factory-license.json` by late commands.

## Required USB Root Files

Place these files at the root of the Ubuntu installer media:

```text
user-data
meta-data
post-install.sh
drycontroller-edge-post-install.service
wifi.env              # optional, copied from wifi.env.example and filled by operator/team
drycontroller-edge.tar.gz
drycontroller-apt/
bundle-images.tar     # optional but recommended for clean-room first boot
tailnet.env           # required for factory activation media
factory-license.json  # required for factory activation media
```

`drycontroller-edge.tar.gz` must contain the edge repository. The tar can contain
files directly at the root, or under one top-level `Drycontroller-edge/`
directory.

Build `drycontroller-apt/` on an Ubuntu amd64 host whose apt sources match the
installer ISO release:

```bash
sudo apt-get update
sudo apt-get install -y dpkg-dev
bash scripts/build-autoinstall-offline-apt-repo.sh --out dist/autoinstall/drycontroller-apt
```

Ubuntu 22.04 media should use a jammy package set; Ubuntu 24.04 media should use
a noble package set.

If the default Ubuntu archive is unstable from the build host, pass a reachable
mirror:

```bash
bash scripts/build-autoinstall-offline-apt-repo.sh \
  --mirror https://mirrors.aliyun.com/ubuntu/ \
  --out dist/autoinstall/drycontroller-apt
```

The autoinstall profile does not override Subiquity's apt configuration during
the installer phase. It only verifies that `drycontroller-apt/` exists, copies it
into the target as `/opt/drycontroller-apt`, and points the installed system at
that offline repo for first-boot Docker/runtime package installation.

If `/bundle-images.tar` exists on the installer media, it is copied to
`/opt/drycontroller-images/bundle-images.tar`. First boot loads it with
`docker load` after Docker is installed, then removes the tar to save disk.

## Installer Login And SSH

The source `user-data` intentionally keeps `identity.password` as
`$6$REPLACE_WITH_SHA512_CRYPT_HASH` so no real password or hash is committed. The
`dryops` Linux account password is also the SSH password because the installer
enables password SSH for that account.

For factory/site media, create a local password env file:

```bash
cp infra/autoinstall/password.env.example infra/autoinstall/password.env
chmod 0600 infra/autoinstall/password.env
$EDITOR infra/autoinstall/password.env
bash scripts/build-autoinstall-bundle.sh \
  --password-env-file infra/autoinstall/password.env
```

`password.env` should contain:

```text
DRYOPS_PASSWORD=replace-with-strong-password
```

The bundle script reads the plaintext password locally, converts it to a
sha512-crypt hash with `openssl passwd -6 -stdin`, and writes only the hash into
`dist/autoinstall/user-data`. `password.env` is ignored by git and excluded from
  `drycontroller-edge.tar.gz`.

`tailnet.env` and `factory-license.json` are batch materials. They are not
generated by this directory. Place them at the installer media root, or put them
under `dist/autoinstall/` before remastering with OS Installer USB Maker. Token
or factory license rotation should replace these files without rebuilding the
edge source bundle or offline apt repository.

If you already generated a sha512-crypt hash, you can still inject it directly:

```bash
openssl passwd -6 > /tmp/drycontroller-autoinstall.passhash
bash scripts/build-autoinstall-bundle.sh \
  --password-hash-file /tmp/drycontroller-autoinstall.passhash
```

Do not write or remaster media while `dist/autoinstall/user-data` still contains
`REPLACE_WITH_SHA512_CRYPT_HASH`; the `dryops` console login and password SSH
login will not work.

The installer profile installs SSH, allows password SSH for the `dryops` account,
and enables host SSH during late commands. This is kept available for development,
operations, and field testing on the trusted LAN.

## macOS USB Write Note

When writing the installer image with `dd` on macOS, write to the raw device
such as `/dev/rdisk2`, not the buffered device `/dev/disk2`. A write through
`/dev/disk2` was observed at about 148 KB/s and took about 5 hours; `/dev/rdisk2`
is typically 10-20x faster.

```bash
diskutil list
diskutil unmountDisk /dev/disk2
sudo dd if=ubuntu-server.iso of=/dev/rdisk2 bs=4m status=progress
sync
diskutil eject /dev/disk2
```

Verify the disk number before running `dd`; the command overwrites the target
device.

## Boot Parameters

Ubuntu Server autoinstall needs the installer kernel command line to include:

```text
autoinstall ds=nocloud;s=/cdrom/
```

Most ISO remastering tools can add this to the GRUB menu entry. For a first
factory validation, it is acceptable to edit the GRUB command line manually once
at boot and then bake the final media after the flow is confirmed.

## Storage Policy

`user-data` uses explicit curtin storage actions instead of the default
`direct` layout:

- select the largest non-installer disk;
- wipe the target disk with `superblock-recursive`;
- create a GPT table with `bios_grub`, EFI, and ext4 root partitions;
- mount the EFI System Partition at `/boot/efi`;
- mount the remaining ext4 partition at `/`;
- disable swap.

For IPCs with multiple internal disks, replace the `match` rule in `user-data`
with a serial/model/path match before factory use.
