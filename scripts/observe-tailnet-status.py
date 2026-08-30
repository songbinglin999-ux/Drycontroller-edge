#!/usr/bin/python3
"""Persist one sanitized read-only Tailnet observation for edge-api."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path


RUNTIME_ROOT = Path("/usr/libexec/drycontroller")
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from common.tailscale_status import read_tailscale_status  # noqa: E402


OUTPUT = Path("/run/drycontroller-tailnet-observation/status.json")


def _edge_api_ingress_active() -> bool:
    completed = subprocess.run(
        ["/usr/bin/systemctl", "is-active", "--quiet", "drycontroller-tailnet-edge-api.socket"],
        check=False,
        timeout=5,
        env={"PATH": "/usr/bin:/bin"},
    )
    return completed.returncode == 0


def observe(output: Path = OUTPUT) -> dict[str, object]:
    observation = read_tailscale_status()
    detail = observation.get("detail")
    if not isinstance(detail, dict):
        detail = {}
        observation["detail"] = detail
    detail["edge_api_ingress_active"] = _edge_api_ingress_active()
    payload = {
        "observation": observation,
        "observed_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "schema": "drycontroller.tailnet-observation.v1",
    }
    output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return payload


if __name__ == "__main__":
    observe()
