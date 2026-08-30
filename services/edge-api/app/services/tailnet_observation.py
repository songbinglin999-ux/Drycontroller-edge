"""Read the fixed root-owned sanitized Tailnet observation."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any


OBSERVATION_PATH = Path("/run/drycontroller-tailnet-observation/status.json")
MAX_BYTES = 64 * 1024


def load_tailnet_observation(path: Path = OBSERVATION_PATH) -> dict[str, Any]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o444
        or metadata.st_size <= 0
        or metadata.st_size > MAX_BYTES
    ):
        raise ValueError("tailnet observation is unsafe")
    payload = json.loads(path.read_bytes())
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "observed_at", "observation"}
        or payload.get("schema") != "drycontroller.tailnet-observation.v1"
        or not isinstance(payload.get("observed_at"), str)
        or not isinstance(payload.get("observation"), dict)
    ):
        raise ValueError("tailnet observation is invalid")
    observation = payload["observation"]
    if set(observation) != {"ok", "reason", "detail"} or not isinstance(
        observation.get("detail"), dict
    ):
        raise ValueError("tailnet observation is invalid")
    return observation
