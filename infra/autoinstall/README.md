# DryController Edge OS Installer

This directory is the public-only autoinstall overlay for the RB-6 candidate.
It installs no reusable login credential or device secret. First boot creates
per-device runtime secrets, requires a real TPM 2.0 resource manager, starts the
isolated factory identity endpoint, and commits one signed H1/T1/S1 pair.
The public overlay also carries the Cloud-selected, checksum-locked Tailscale
amd64 DEB and enables host `tailscaled`; it never carries a Tailnet auth key.

The only formal build entrypoint is `scripts/build-os-installer-release.sh` and
the only USB write entrypoint is `scripts/write-os-installer-usb.sh`. Do not run
either while the RB-5B/RB-5C hardware and independent DANGER review gates remain
open in the workspace audit. Do not hand-run ISO or disk tooling.
