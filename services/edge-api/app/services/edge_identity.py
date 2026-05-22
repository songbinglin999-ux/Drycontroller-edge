from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def load_edge_identity() -> dict[str, str | None]:
    path = Path(
        os.getenv("DRYCONTROLLER_DEVICE_JSON")
        or os.getenv("PREFLIGHT_DEVICE_JSON")
        or "/var/lib/drycontroller/device.json"
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return {
        "edge_device_id": _clean(data.get("device_id")),
        "edge_node_name": _clean(data.get("node_name")),
    }
