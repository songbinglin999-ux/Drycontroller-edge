#!/usr/bin/python3
"""Record the live boot TPM identity without exposing private material."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import sys


IDENTITY_ROOT = Path("/opt/drycontroller-identity")
if str(IDENTITY_ROOT) not in sys.path:
    sys.path.insert(0, str(IDENTITY_ROOT))

from identity_service.backend import TpmIdentityBackend  # noqa: E402


OUTPUT = Path("/run/drycontroller-identity/tpm-observation.json")


def canonical_json(payload: dict[str, str]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def observe(output: Path = OUTPUT) -> dict[str, str]:
    health = TpmIdentityBackend().health()
    fingerprint = health.get("identity_fingerprint")
    if health.get("status") != "ok" or not isinstance(fingerprint, str):
        raise RuntimeError("live TPM identity unavailable")
    payload = {
        "identity_fingerprint": fingerprint,
        "observed_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "schema": "drycontroller.tpm-identity-observation.v1",
        "status": "ok",
    }
    output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        directory = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return payload


if __name__ == "__main__":
    try:
        observe()
    except (OSError, RuntimeError) as exc:
        print(f"TPM identity observation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
