from __future__ import annotations

import datetime as dt
import re
from typing import Any

from app.core.config import settings


_TRANSACTION_ID = re.compile(r"^[0-9a-f]{32}$")


def _normalize_registry_host(value: str) -> str:
    host = value.strip().rstrip("/")
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    return host.rstrip("/")


def _parse_ts(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def normalize_registry_credential(payload: dict[str, Any]) -> dict[str, Any]:
    registry_host = _normalize_registry_host(str(payload.get("registry_host") or ""))
    username = str(payload.get("username") or "").strip()
    secret = str(payload.get("secret") or "")
    credential_id = str(payload.get("credential_id") or "").strip()
    secret_fingerprint = str(payload.get("secret_fingerprint") or "").strip()
    version = int(payload.get("version") or 0)
    if not registry_host or not username or not secret or not credential_id or not secret_fingerprint or version < 1:
        raise ValueError("registry_credential_payload_incomplete")
    return {
        "registry_host": registry_host,
        "username": username,
        "secret": secret,
        "credential_id": credential_id,
        "secret_fingerprint": secret_fingerprint,
        "version": version,
    }


def registry_credential_check(
    data: object,
    *,
    expected_credential_id: str | None = None,
    expected_version: int | None = None,
    expected_secret_fingerprint: str | None = None,
) -> dict[str, Any]:
    if not isinstance(data, dict):
        data = {"status": "missing"}
    if data.get("status") != "active":
        return {"ok": False, "reason": "registry_credential_missing", "detail": data}

    missing = [
        key
        for key in (
            "credential_id",
            "registry_host",
            "username",
            "secret_fingerprint",
            "version",
            "transaction_id",
            "audit_id",
            "updated_at",
        )
        if not data.get(key)
    ]
    if missing:
        return {"ok": False, "reason": "registry_credential_status_incomplete", "detail": {**data, "missing": missing}}
    if (
        not isinstance(data.get("audit_id"), int)
        or isinstance(data.get("audit_id"), bool)
        or data["audit_id"] < 1
    ):
        return {"ok": False, "reason": "registry_credential_audit_invalid", "detail": data}
    if (
        not isinstance(data.get("transaction_id"), str)
        or _TRANSACTION_ID.fullmatch(data["transaction_id"]) is None
    ):
        return {"ok": False, "reason": "registry_credential_transaction_invalid", "detail": data}
    if (
        isinstance(data.get("version"), bool)
        or not isinstance(data.get("version"), int)
        or data["version"] < 1
    ):
        return {"ok": False, "reason": "registry_credential_version_invalid", "detail": data}

    if expected_credential_id and data.get("credential_id") != expected_credential_id:
        return {"ok": False, "reason": "registry_credential_id_mismatch", "detail": data}
    if expected_version is not None:
        try:
            current_version = int(data.get("version") or 0)
        except (TypeError, ValueError):
            current_version = 0
        if current_version < int(expected_version):
            return {"ok": False, "reason": "registry_credential_version_stale", "detail": data}
    if expected_secret_fingerprint and data.get("secret_fingerprint") != expected_secret_fingerprint:
        return {"ok": False, "reason": "registry_credential_fingerprint_mismatch", "detail": data}

    max_age_hours = float(settings.REGISTRY_CREDENTIAL_MAX_AGE_HOURS or 0)
    updated_at = _parse_ts(data.get("updated_at"))
    if updated_at is None:
        return {"ok": False, "reason": "registry_credential_updated_at_invalid", "detail": data}
    if max_age_hours > 0 and updated_at is not None:
        age_hours = (dt.datetime.now(dt.timezone.utc) - updated_at).total_seconds() / 3600
        if age_hours > max_age_hours:
            return {"ok": False, "reason": "registry_credential_stale", "detail": {**data, "age_hours": age_hours}}

    return {"ok": True, "reason": "ok", "detail": data}
