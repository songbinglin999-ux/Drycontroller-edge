# Edge OS autoinstall status

Production OS Installer builds are intentionally blocked in RB-5A. The old
shared factory credential route was deleted and is not a supported fallback.
RB-6 will define and verify the public-material-only installer after RB-5B
finishes the TPM identity backend.

Until then, do not build, write, or deploy factory media from this branch.
