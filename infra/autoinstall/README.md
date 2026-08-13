# DryController Edge OS Installer

The autoinstall source remains as an RB-6 input, but RB-5A cannot produce a
release ISO. The retired shared factory credential and host activation services
have been removed. `scripts/build-os-installer-release.sh` therefore fails
closed until RB-5B supplies the reviewed TPM backend and RB-6 rebuilds media
using public material only.

Do not hand-run ISO tooling or restore secret-bearing installer inputs.
