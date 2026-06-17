"""
Factory activation state helpers.

Factory activation writes are owned by the host-level
`drycontroller-activation.service` path. Edge API only reports the resulting
state so operators and Cloud Ops can inspect activation outcome without a
second activation implementation.
"""

from __future__ import annotations

import hashlib
import json
import os
from enum import Enum
from pathlib import Path
from typing import Any

from app.core.config import settings


class ActivationState(str, Enum):
    FACTORY_INSTALLED = "factory_installed"
    BOOTSTRAP_TAILNET_JOINED = "bootstrap_tailnet_joined"
    PENDING_OPS_APPROVAL = "pending_ops_approval"
    CLOUD_ACTIVATED = "cloud_activated"
    FORMAL_TAILNET_JOINED = "formal_tailnet_joined"
    FORMAL_LICENSE_ACTIVE = "formal_license_active"


_STATE_FILE = Path(os.getenv("DRYCONTROLLER_ACTIVATION_STATE", "/var/lib/drycontroller/activation-state.json"))
_FACTORY_LICENSE_PATH = Path("/etc/drycontroller/license/license.json")
_FACTORY_LICENSE_ARCHIVE_PATH = Path("/var/lib/drycontroller/factory-license.json")
_FACTORY_LICENSE_PUB_PATH = Path("/etc/drycontroller/keys/license.pub")
_DEVICE_JSON_PATH = Path("/var/lib/drycontroller/device.json")
_TAILSCALE_CONFIG_DIR = Path("/var/lib/tailscale")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _machine_id() -> str:
    try:
        return Path("/etc/machine-id").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _license_fingerprint(license_path: Path, machine_id: str) -> str:
    if not license_path.is_file():
        return ""
    payload = _read_json(license_path)
    if not payload or not machine_id:
        return ""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    h = hashlib.sha256()
    h.update(raw)
    h.update(b"\0")
    h.update(machine_id.encode("utf-8"))
    return h.hexdigest()


def _factory_license_source_path() -> Path:
    return _FACTORY_LICENSE_ARCHIVE_PATH if _FACTORY_LICENSE_ARCHIVE_PATH.is_file() else _FACTORY_LICENSE_PATH


def get_activation_state() -> dict[str, Any]:
    """Return current activation state and read-only metadata."""
    state_data = _read_json(_STATE_FILE)
    current_state = state_data.get("state", ActivationState.FACTORY_INSTALLED.value)
    machine_id = _machine_id()

    factory_license_source = _factory_license_source_path()
    factory_license_exists = factory_license_source.is_file()

    return {
        "state": current_state,
        "factory_license_exists": factory_license_exists,
        "factory_pub_exists": _FACTORY_LICENSE_PUB_PATH.is_file(),
        "device_json_exists": _DEVICE_JSON_PATH.is_file(),
        "tailscale_state_exists": (_TAILSCALE_CONFIG_DIR / "tailscaled.state").is_file(),
        "machine_id": machine_id,
        "factory_license_fingerprint": (
            _license_fingerprint(factory_license_source, machine_id) if factory_license_exists else None
        ),
        "cloud_ops_base_url": settings.CLOUD_OPS_BASE_URL or None,
        "ts_login_server": settings.TS_LOGIN_SERVER or None,
        **{k: v for k, v in state_data.items() if k != "state"},
    }
