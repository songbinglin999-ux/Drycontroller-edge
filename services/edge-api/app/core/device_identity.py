from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from app.core.config import settings


class DeviceIdentityUnavailable(RuntimeError):
    pass


def load_runtime_identity(
    path: Path | None = None,
    *,
    require_root_owner: bool = True,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    identity_path = path or Path(settings.DEVICE_IDENTITY_PATH)
    try:
        stat = identity_path.stat()
        if stat.st_size > 65_536:
            raise DeviceIdentityUnavailable("device identity state is too large")
        if stat.st_mode & 0o077:
            raise DeviceIdentityUnavailable("device identity state must be 0600")
        if require_root_owner and stat.st_uid != 0:
            raise DeviceIdentityUnavailable("device identity state must be root-owned")
        payload = json.loads(identity_path.read_text(encoding="utf-8"))
    except DeviceIdentityUnavailable:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeviceIdentityUnavailable("device identity state is unavailable") from exc
    required = {
        "schema",
        "device_id",
        "tenant_id",
        "line_id",
        "node_name",
        "identity_fingerprint",
        "runtime_credential",
        "runtime_credential_hash",
        "credential_expires_at",
    }
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "drycontroller.device-identity.v1"
        or any(not isinstance(payload.get(field), str) or not payload[field] for field in required)
        or payload.get("node_name") != str(payload.get("device_id", "")).lower()
        or hashlib.sha256(payload["runtime_credential"].encode("utf-8")).hexdigest()
        != payload["runtime_credential_hash"]
    ):
        raise DeviceIdentityUnavailable("device identity state is invalid")
    try:
        expires_at = dt.datetime.fromisoformat(payload["credential_expires_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeviceIdentityUnavailable("device credential expiry is invalid") from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.timezone.utc)
    current = now or dt.datetime.now(dt.timezone.utc)
    if expires_at.astimezone(dt.timezone.utc) <= current.astimezone(dt.timezone.utc):
        raise DeviceIdentityUnavailable("device credential is expired")
    return payload
