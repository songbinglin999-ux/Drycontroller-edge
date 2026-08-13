from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import uuid


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


def canonical_challenge(challenge: dict[str, object]) -> bytes:
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
    if challenge.get("operation") == "host_release":
        required |= {
            "tenant_id",
            "line_id",
            "node_name",
            "node_id",
            "job_id",
            "job_item_id",
            "host_release_id",
            "host_manifest_digest",
            "software_release_id",
            "software_manifest_digest",
        }
    if set(challenge) != required:
        raise IdentityChallengeError("identity challenge fields do not match the fixed schema")
    if challenge["schema"] != CHALLENGE_SCHEMA or challenge["domain"] != CHALLENGE_DOMAIN:
        raise IdentityChallengeError("identity challenge domain or schema mismatch")
    if challenge["operation"] not in {"activate", "rotate", "re_register", "host_release"}:
        raise IdentityChallengeError("invalid identity operation")
    nonce = challenge["challenge_nonce"]
    if not isinstance(nonce, str):
        raise IdentityChallengeError("invalid challenge nonce")
    decode_nonce(nonce)
    for field in ("identity_fingerprint", "runtime_credential_hash"):
        value = challenge[field]
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise IdentityChallengeError(f"invalid {field}")
    if challenge["operation"] == "host_release":
        for field in ("tenant_id", "line_id", "node_name"):
            if not isinstance(challenge[field], str) or not challenge[field]:
                raise IdentityChallengeError("invalid host release challenge binding")
        for field in ("node_id", "job_id", "job_item_id"):
            value = challenge[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise IdentityChallengeError("invalid host release challenge binding")
        for field in ("host_release_id", "software_release_id"):
            value = challenge[field]
            try:
                canonical = str(uuid.UUID(value)) if isinstance(value, str) else None
            except ValueError as exc:
                raise IdentityChallengeError("invalid host release challenge binding") from exc
            if canonical != value:
                raise IdentityChallengeError("invalid host release challenge binding")
        for field in ("host_manifest_digest", "software_manifest_digest"):
            value = challenge[field]
            if (
                not isinstance(value, str)
                or len(value) != 71
                or not value.startswith("sha256:")
                or any(char not in "0123456789abcdef" for char in value[7:])
            ):
                raise IdentityChallengeError("invalid host release challenge binding")
    payload = dict(challenge)
    expires_at = challenge["expires_at"]
    if not isinstance(expires_at, str):
        raise IdentityChallengeError("invalid challenge expiry")
    payload["expires_at"] = format_timestamp(parse_timestamp(expires_at))
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
