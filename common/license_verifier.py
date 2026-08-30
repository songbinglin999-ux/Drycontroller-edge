from __future__ import annotations

import base64
import binascii
import datetime as dt
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


LICENSE_TRUST_FIELDS = {
    "schema",
    "key_id",
    "algorithm",
    "public_key_spki_der_b64",
    "public_key_sha256",
}
LICENSE_TRUST_SCHEMA = "drycontroller.license-trust.v1"
LICENSE_TRUST_POLICY_SCHEMA = "drycontroller.license-trust-policy.v1"
LICENSE_SCHEMA = "drycontroller.license.v2"
LICENSE_PRODUCT = "edge-app-v1"
LICENSE_CAPABILITIES = ["cloud-session", "edge-app-ota", "production-control"]
TPM_OBSERVATION_SCHEMA = "drycontroller.tpm-identity-observation.v1"
LICENSE_FIELDS = {
    "schema",
    "license_id",
    "previous_license_id",
    "key_id",
    "product",
    "device_id",
    "tenant_id",
    "line_id",
    "node_name",
    "identity_fingerprint",
    "capabilities",
    "issued_at",
    "expires_at",
    "issuer",
    "signature_alg",
    "signature",
}
KEY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
LICENSE_ID_RE = re.compile(r"lic_[a-f0-9]{32}")
FINGERPRINT_RE = re.compile(r"[a-f0-9]{64}")


@dataclass(frozen=True)
class LicenseTrust:
    key_id: str
    public_key_raw: bytes
    public_key_spki_der: bytes
    canonical_sha256: str


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def parse_license_trust(raw: bytes) -> LicenseTrust:
    if not raw or len(raw) > 16 * 1024:
        raise ValueError("license trust artifact size is invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("license trust artifact is invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != LICENSE_TRUST_FIELDS:
        raise ValueError("license trust artifact fields are invalid")
    if _canonical_json_bytes(payload) != raw:
        raise ValueError("license trust artifact must use canonical JSON bytes")
    if payload.get("schema") != LICENSE_TRUST_SCHEMA:
        raise ValueError("license trust schema is unsupported")
    if payload.get("algorithm") != "Ed25519":
        raise ValueError("license trust algorithm is unsupported")
    key_id = payload.get("key_id")
    if not isinstance(key_id, str) or KEY_ID_RE.fullmatch(key_id) is None:
        raise ValueError("license trust key_id is invalid")
    encoded_spki = payload.get("public_key_spki_der_b64")
    if not isinstance(encoded_spki, str):
        raise ValueError("license trust SPKI is invalid")
    try:
        spki = base64.b64decode(encoded_spki, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("license trust SPKI is invalid") from exc
    if base64.b64encode(spki).decode("ascii") != encoded_spki:
        raise ValueError("license trust SPKI must use padded standard Base64")
    try:
        public_key = serialization.load_der_public_key(spki)
    except ValueError as exc:
        raise ValueError("license trust SPKI is invalid") from exc
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("license trust SPKI is not Ed25519")
    canonical_spki = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if canonical_spki != spki:
        raise ValueError("license trust SPKI is not canonical DER")
    expected_digest = "sha256:" + hashlib.sha256(spki).hexdigest()
    if payload.get("public_key_sha256") != expected_digest:
        raise ValueError("license trust public-key digest mismatch")
    public_key_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return LicenseTrust(
        key_id=key_id,
        public_key_raw=public_key_raw,
        public_key_spki_der=spki,
        canonical_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
    )


def load_license_trust(path: Path) -> LicenseTrust:
    return parse_license_trust(path.read_bytes())


def _validate_policy_binding(value: Any, label: str) -> dict[str, str] | None:
    if value is None and label == "previous":
        return None
    if not isinstance(value, dict) or set(value) != {"canonical_sha256", "key_id"}:
        raise ValueError(f"license trust policy {label} binding is invalid")
    key_id = value.get("key_id")
    digest = value.get("canonical_sha256")
    if not isinstance(key_id, str) or KEY_ID_RE.fullmatch(key_id) is None:
        raise ValueError(f"license trust policy {label} key_id is invalid")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise ValueError(f"license trust policy {label} digest is invalid")
    return {"key_id": key_id, "canonical_sha256": digest}


def load_license_trust_policy(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if not raw or len(raw) > 16 * 1024:
        raise ValueError("license trust policy size is invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("license trust policy is invalid JSON") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "active", "previous"}
        or payload.get("schema") != LICENSE_TRUST_POLICY_SCHEMA
    ):
        raise ValueError("license trust policy fields are invalid")
    if _canonical_json_bytes(payload) != raw:
        raise ValueError("license trust policy must use canonical JSON bytes")
    return {
        "schema": LICENSE_TRUST_POLICY_SCHEMA,
        "active": _validate_policy_binding(payload.get("active"), "active"),
        "previous": _validate_policy_binding(payload.get("previous"), "previous"),
    }


def load_active_license_trust(policy_path: Path) -> LicenseTrust:
    policy = load_license_trust_policy(policy_path)
    active = policy["active"]
    assert isinstance(active, dict)
    artifact_path = policy_path.parent / "versions" / active["key_id"] / "license-trust.json"
    trust = load_license_trust(artifact_path)
    if trust.key_id != active["key_id"] or trust.canonical_sha256 != active["canonical_sha256"]:
        raise ValueError("active license trust artifact does not match policy")
    return trust


def canonical_license_bytes(payload: dict[str, Any]) -> bytes:
    unsigned = {k: v for k, v in payload.items() if k != "signature"}
    return json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_signature(payload: dict[str, Any], public_key: bytes) -> None:
    if len(public_key) != 32:
        raise ValueError("public key must be 32 bytes")
    try:
        sig = base64.b64decode(str(payload.get("signature", "")), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("signature must be base64 encoded") from exc
    if len(sig) != 64:
        raise ValueError("signature must decode to 64 bytes")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(sig, canonical_license_bytes(payload))
    except InvalidSignature as exc:
        raise ValueError("signature_invalid") from exc


def verify_license(payload: dict[str, Any], trust: LicenseTrust) -> None:
    validate_license_v2(payload)
    if payload.get("key_id") != trust.key_id:
        raise ValueError("license key_id does not match active trust policy")
    if payload.get("signature_alg") != "ed25519":
        raise ValueError("license signature algorithm is unsupported")
    verify_signature(payload, trust.public_key_raw)


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("license file must contain a JSON object")
    return data


def parse_dt(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def validate_license_v2(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != LICENSE_FIELDS:
        raise ValueError("license fields do not match the v2 schema")
    if payload.get("schema") != LICENSE_SCHEMA or payload.get("product") != LICENSE_PRODUCT:
        raise ValueError("unsupported license schema or product")
    license_id = payload.get("license_id")
    if not isinstance(license_id, str) or LICENSE_ID_RE.fullmatch(license_id) is None:
        raise ValueError("license_id is invalid")
    previous = payload.get("previous_license_id")
    if previous is not None and (
        not isinstance(previous, str) or LICENSE_ID_RE.fullmatch(previous) is None
    ):
        raise ValueError("previous_license_id is invalid")
    key_id = payload.get("key_id")
    if not isinstance(key_id, str) or KEY_ID_RE.fullmatch(key_id) is None:
        raise ValueError("license key_id is invalid")
    for field in ("device_id", "tenant_id", "line_id", "node_name"):
        value = payload.get(field)
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError(f"license {field} is invalid")
    fingerprint = payload.get("identity_fingerprint")
    if not isinstance(fingerprint, str) or FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise ValueError("license identity_fingerprint is invalid")
    if payload.get("capabilities") != LICENSE_CAPABILITIES:
        raise ValueError("license capabilities do not match edge-app-v1")
    issued_at = parse_dt(str(payload.get("issued_at") or ""))
    expires_at = parse_dt(str(payload.get("expires_at") or ""))
    if expires_at <= issued_at:
        raise ValueError("license expires_at must follow issued_at")
    if payload.get("issuer") != "drycontroller-cloud-ops":
        raise ValueError("license issuer is invalid")
    if payload.get("signature_alg") != "ed25519":
        raise ValueError("license signature algorithm is unsupported")
    return payload


def load_tpm_observation(path: Path) -> dict[str, Any]:
    payload = load_json(path)
    if set(payload) != {"schema", "status", "identity_fingerprint", "observed_at"}:
        raise ValueError("TPM identity observation fields are invalid")
    if payload.get("schema") != TPM_OBSERVATION_SCHEMA or payload.get("status") != "ok":
        raise ValueError("TPM identity observation is not healthy")
    fingerprint = payload.get("identity_fingerprint")
    if not isinstance(fingerprint, str) or FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise ValueError("TPM identity observation fingerprint is invalid")
    parse_dt(str(payload.get("observed_at") or ""))
    return payload


def license_runtime_error(
    payload: dict[str, Any],
    *,
    device_identity: dict[str, Any],
    tpm_observation: dict[str, Any],
    required_capability: str | None = None,
    now: dt.datetime | None = None,
) -> str | None:
    try:
        validate_license_v2(payload)
        expires_at = parse_dt(str(payload["expires_at"]))
        issued_at = parse_dt(str(payload["issued_at"]))
    except (KeyError, TypeError, ValueError):
        return "malformed_license"
    current = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    if issued_at > current:
        return "not_yet_valid"
    if expires_at <= current:
        return "expired"
    if (
        tpm_observation.get("schema") != TPM_OBSERVATION_SCHEMA
        or tpm_observation.get("status") != "ok"
        or payload["identity_fingerprint"] != tpm_observation.get("identity_fingerprint")
    ):
        return "tpm_identity_mismatch"
    for field in ("device_id", "tenant_id", "line_id", "node_name", "identity_fingerprint"):
        if payload[field] != device_identity.get(field):
            return "device_identity_mismatch"
    if required_capability is not None and required_capability not in payload["capabilities"]:
        return "capability_missing"
    return None


def verify_installed_license(
    *,
    license_path: Path,
    trust_policy_path: Path,
    device_identity_path: Path,
    tpm_observation_path: Path,
    required_capability: str | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    try:
        payload = load_json(license_path)
        trust = load_active_license_trust(trust_policy_path)
        device_identity = load_json(device_identity_path)
        observation = load_tpm_observation(tpm_observation_path)
        verify_license(payload, trust)
        runtime_error = license_runtime_error(
            payload,
            device_identity=device_identity,
            tpm_observation=observation,
            required_capability=required_capability,
            now=now,
        )
        if runtime_error is not None:
            raise ValueError(runtime_error)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {"valid": False, "reason": str(exc)}
    return {
        "valid": True,
        "reason": "ok",
        "license_id": payload["license_id"],
        "product": payload["product"],
        "device_id": payload["device_id"],
        "identity_fingerprint": payload["identity_fingerprint"],
        "expires_at": payload["expires_at"],
        "capabilities": payload["capabilities"],
    }
