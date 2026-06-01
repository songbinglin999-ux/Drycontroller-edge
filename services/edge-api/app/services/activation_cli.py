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


def main() -> int:
    # Ensure the edge-api app module is importable.
    # When run via systemd, WorkingDirectory=/opt/drycontroller/edge-api so no path hack needed.
    # When run manually from the repo, walk up to services/edge-api/.
    from pathlib import Path as _Path
    _here = _Path(__file__).resolve()
    # app/services/activation_cli.py -> app -> edge-api
    _edge_api_dir = _here.parent.parent.parent
    if (_edge_api_dir / "app" / "main.py").is_file():
        import sys as _sys
        _sys.path.insert(0, str(_edge_api_dir))

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
