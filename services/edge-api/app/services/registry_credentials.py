from __future__ import annotations

import base64
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from app.core.config import settings


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _normalize_registry_host(value: str) -> str:
    host = value.strip().rstrip("/")
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    return host.rstrip("/")


def _write_json_atomic(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


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


def install_registry_credential(payload: dict[str, Any]) -> dict[str, Any]:
    registry_host = _normalize_registry_host(str(payload.get("registry_host") or ""))
    username = str(payload.get("username") or "").strip()
    secret = str(payload.get("secret") or "")
    credential_id = str(payload.get("credential_id") or "").strip()
    secret_fingerprint = str(payload.get("secret_fingerprint") or "").strip()
    version = int(payload.get("version") or 0)
    if not registry_host or not username or not secret or not credential_id or not secret_fingerprint or version < 1:
        raise ValueError("registry_credential_payload_incomplete")

    docker_config_path = Path(settings.REGISTRY_DOCKER_CONFIG_PATH)
    docker_config = _read_json(docker_config_path)
    auths = docker_config.get("auths")
    if not isinstance(auths, dict):
        auths = {}
    auth = base64.b64encode(f"{username}:{secret}".encode("utf-8")).decode("ascii")
    auths[registry_host] = {"auth": auth}
    docker_config["auths"] = auths
    _write_json_atomic(docker_config_path, docker_config)

    status_payload = {
        "status": "active",
        "credential_id": credential_id,
        "registry_host": registry_host,
        "username": username,
        "secret_fingerprint": secret_fingerprint,
        "version": version,
        "updated_at": _now().isoformat(),
        "docker_config_path": str(docker_config_path),
    }
    _write_json_atomic(Path(settings.REGISTRY_CREDENTIAL_STATUS_PATH), status_payload)
    return status_payload


def registry_credential_status() -> dict[str, Any]:
    status_path = Path(settings.REGISTRY_CREDENTIAL_STATUS_PATH)
    data = _read_json(status_path)
    if not data:
        return {"status": "missing", "status_path": str(status_path)}
    data = dict(data)
    data["status_path"] = str(status_path)
    data["docker_config_exists"] = Path(settings.REGISTRY_DOCKER_CONFIG_PATH).is_file()
    return data


def _docker_config_has_auth(registry_host: str) -> bool:
    docker_config = _read_json(Path(settings.REGISTRY_DOCKER_CONFIG_PATH))
    auths = docker_config.get("auths")
    if not isinstance(auths, dict):
        return False
    entry = auths.get(registry_host)
    return isinstance(entry, dict) and bool(entry.get("auth"))


def registry_credential_check(
    *,
    expected_credential_id: str | None = None,
    expected_version: int | None = None,
    expected_secret_fingerprint: str | None = None,
) -> dict[str, Any]:
    data = registry_credential_status()
    if data.get("status") != "active":
        return {"ok": False, "reason": "registry_credential_missing", "detail": data}
    if not Path(settings.REGISTRY_DOCKER_CONFIG_PATH).is_file():
        return {"ok": False, "reason": "registry_docker_config_missing", "detail": data}

    missing = [
        key
        for key in ("credential_id", "registry_host", "username", "secret_fingerprint", "version", "updated_at")
        if not data.get(key)
    ]
    if missing:
        return {"ok": False, "reason": "registry_credential_status_incomplete", "detail": {**data, "missing": missing}}
    if not _docker_config_has_auth(str(data.get("registry_host") or "")):
        return {"ok": False, "reason": "registry_docker_config_auth_missing", "detail": data}

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
    if max_age_hours > 0 and updated_at is not None:
        age_hours = (_now() - updated_at).total_seconds() / 3600
        if age_hours > max_age_hours:
            return {"ok": False, "reason": "registry_credential_stale", "detail": {**data, "age_hours": age_hours}}

    return {"ok": True, "reason": "ok", "detail": data}
