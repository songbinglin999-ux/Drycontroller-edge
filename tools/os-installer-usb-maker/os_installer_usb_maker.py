#!/usr/bin/env python3
"""Fail-closed placeholder until the RB-6 public-material installer is built."""

import sys


def main() -> int:
    print(
        "OS Installer USB Maker is disabled on RB-5A; production media resumes in RB-6 after RB-5B TPM review.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
