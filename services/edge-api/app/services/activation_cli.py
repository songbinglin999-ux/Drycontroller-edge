#!/usr/bin/env python3
"""
CLI entry point for drycontroller-activation.service.

Runs the factory activation flow on first boot.
Exit 0 = success or already activated.
Exit 1 = failure (systemd will retry on next boot).
"""

import asyncio
import json
import sys
from pathlib import Path


def main() -> int:
    # Ensure the edge-api app module is importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "edge-opencode-factory-license" / "services" / "edge-api"))

    from app.services.activation import get_activation_state, run_activation

    state = get_activation_state()
    if state.get("state") == "formal_license_active":
        print("already_activated")
        return 0

    print(f"current_state: {state.get('state')}")
    print("starting_activation...")

    result = asyncio.run(run_activation())

    print(json.dumps(result, indent=2, default=str))

    if result.get("success"):
        print("activation_complete")
        return 0
    else:
        print(f"activation_failed: {result.get('error')}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
