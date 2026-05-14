from __future__ import annotations

import base64
import datetime as dt
import json
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def canonical_license_bytes(payload: dict[str, Any]) -> bytes:
    unsigned = {k: v for k, v in payload.items() if k != "signature"}
    return json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_signature(payload: dict[str, Any], public_key: bytes) -> None:
    if len(public_key) != 32:
        raise ValueError("public key must be 32 bytes")
    try:
        sig = base64.b64decode(str(payload.get("signature", "")), validate=True)
    except Exception as exc:
        raise ValueError("signature must be base64 encoded") from exc
    if len(sig) != 64:
        raise ValueError("signature must decode to 64 bytes")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(sig, canonical_license_bytes(payload))
    except InvalidSignature as exc:
        raise ValueError("signature_invalid") from exc


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("license file must contain a JSON object")
    return data


def load_public_key(path: Path) -> bytes:
    raw = path.read_text(encoding="utf-8").strip()
    return base64.b64decode(raw, validate=True)


def parse_dt(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)
