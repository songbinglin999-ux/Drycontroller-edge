#!/usr/bin/env python3
"""
Deprecated CLI entry point for the old edge-api activation path.

Factory activation is now owned by the host-level
`/usr/local/sbin/drycontroller-host-activation.sh` systemd path.
"""

import sys


def main() -> int:
    print("deprecated: use /usr/local/sbin/drycontroller-host-activation.sh", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
