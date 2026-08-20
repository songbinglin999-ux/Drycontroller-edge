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

Formal offline apt input is a pre-staged immutable Ubuntu snapshot plus the exact
`drycontroller.offline-apt-input.v1` lock and its independently supplied SHA-256.
The lock binds noble/amd64 Release and Packages metadata, every requested package
version, the full dependency closure, and every DEB digest. Live mirrors,
`current` metadata, dependency re-resolution drift, and online fallback fail
closed. The generated repository carries `input-lock.json` and `receipt.json`.
