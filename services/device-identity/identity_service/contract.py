from __future__ import annotations

import base64
import binascii
import datetime as dt
import json


CHALLENGE_SCHEMA = "drycontroller.device-identity-challenge.v1"
CHALLENGE_DOMAIN = "drycontroller.device-identity"
CLOUD_PROOF_DOMAIN = "drycontroller.device-identity.cloud"
BOOTSTRAP_GRANT_SCHEMA = "drycontroller.device-bootstrap-grant.v1"
ACTIVATION_RECEIPT_SCHEMA = "drycontroller.device-activation-receipt.v1"

_CLOUD_PROOF_FIELDS = {
    BOOTSTRAP_GRANT_SCHEMA: {
        "schema",
        "domain",
        "operation",
        "request_id",
        "device_id",
        "tenant_id",
        "line_id",
        "node_name",
        "identity_fingerprint",
        "bootstrap_token_hash",
        "expires_at",
    },
    ACTIVATION_RECEIPT_SCHEMA: {
        "schema",
        "domain",
        "operation",
        "state",
        "request_id",
        "device_id",
        "tenant_id",
        "line_id",
        "node_name",
        "identity_fingerprint",
        "runtime_credential_hash",
        "credential_expires_at",
    },
}


class IdentityChallengeError(ValueError):
    pass


def as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def parse_timestamp(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IdentityChallengeError("invalid challenge expiry") from exc
    return as_utc(parsed)


def format_timestamp(value: dt.datetime) -> str:
    return as_utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def decode_nonce(value: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise IdentityChallengeError("invalid Base64 nonce") from exc
    if len(raw) < 32:
        raise IdentityChallengeError("nonce must contain at least 256 bits")
    return raw


def canonical_challenge(challenge: dict[str, str]) -> bytes:
    required = {
        "schema",
        "domain",
        "operation",
        "device_id",
        "challenge_id",
        "challenge_nonce",
        "request_id",
        "identity_fingerprint",
        "runtime_credential_hash",
        "expires_at",
    }
    if set(challenge) != required:
        raise IdentityChallengeError("identity challenge fields do not match the fixed schema")
    if challenge["schema"] != CHALLENGE_SCHEMA or challenge["domain"] != CHALLENGE_DOMAIN:
        raise IdentityChallengeError("identity challenge domain or schema mismatch")
    if challenge["operation"] not in {"activate", "rotate", "re_register", "host_release"}:
        raise IdentityChallengeError("invalid identity operation")
    decode_nonce(challenge["challenge_nonce"])
    for field in ("identity_fingerprint", "runtime_credential_hash"):
        value = challenge[field]
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise IdentityChallengeError(f"invalid {field}")
    payload = dict(challenge)
    payload["expires_at"] = format_timestamp(parse_timestamp(challenge["expires_at"]))
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_cloud_proof(payload: dict[str, str]) -> bytes:
    schema = payload.get("schema")
    required = _CLOUD_PROOF_FIELDS.get(schema)
    if not required or set(payload) != required or payload.get("domain") != CLOUD_PROOF_DOMAIN:
        raise IdentityChallengeError("invalid Cloud identity proof schema")
    expected_operation = "bootstrap" if schema == BOOTSTRAP_GRANT_SCHEMA else "activate"
    if payload.get("operation") != expected_operation:
        raise IdentityChallengeError("invalid Cloud identity proof operation")
    if schema == ACTIVATION_RECEIPT_SCHEMA and payload.get("state") != "active":
        raise IdentityChallengeError("invalid Cloud activation receipt state")
    if any(not isinstance(value, str) or not value for value in payload.values()):
        raise IdentityChallengeError("invalid Cloud identity proof value")
    for field in ("identity_fingerprint", "bootstrap_token_hash", "runtime_credential_hash"):
        if field not in payload:
            continue
        value = payload[field]
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise IdentityChallengeError(f"invalid Cloud identity proof {field}")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
