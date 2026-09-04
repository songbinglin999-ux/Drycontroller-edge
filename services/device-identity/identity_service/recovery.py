from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Protocol
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .backend import DeviceIdentityBackend, load_backend
from .contract import (
    ACTIVATION_RECEIPT_SCHEMA,
    CHALLENGE_DOMAIN,
    CHALLENGE_SCHEMA,
    CLOUD_PROOF_DOMAIN,
    IdentityChallengeError,
    canonical_challenge,
    canonical_cloud_proof,
    parse_timestamp,
)
from .state import IdentityStateError, IdentityStateStore


class RecoveryError(RuntimeError):
    pass


class IdentityCloudClient(Protocol):
    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class UrlLibAdminCloudClient:
    def __init__(self, base_url: str, *, username: str, password: str):
        try:
            parsed = urlsplit(base_url)
        except ValueError as exc:
            raise RecoveryError("Cloud identity URL is invalid") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise RecoveryError("Cloud identity URL must use canonical HTTPS")
        self.base_url = base_url.rstrip("/")
        self._opener = urllib.request.build_opener(_RejectRedirect)
        login = self._request(
            "/api/v1/auth/login",
            {"username": username, "password": password},
            bearer=None,
        )
        token = login.get("access_token")
        if not isinstance(token, str) or not token:
            raise RecoveryError("Cloud administrator login did not return a token")
        self._bearer = token

    def _request(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        bearer: str | None,
    ) -> dict[str, Any]:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=15) as response:
                raw = response.read(65_537)
        except urllib.error.HTTPError as exc:
            raise RecoveryError(f"Cloud identity request was rejected (HTTP {exc.code})") from None
        except (OSError, urllib.error.URLError) as exc:
            raise RecoveryError("Cloud identity service is unavailable") from exc
        if len(raw) > 65_536:
            raise RecoveryError("Cloud identity response is too large")
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryError("Cloud identity response is invalid") from exc
        if not isinstance(result, dict):
            raise RecoveryError("Cloud identity response is invalid")
        return result

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(path, payload, bearer=self._bearer)


class CredentialRecovery:
    def __init__(
        self,
        *,
        backend: DeviceIdentityBackend,
        store: IdentityStateStore,
        cloud: IdentityCloudClient,
        cloud_public_key: Ed25519PublicKey,
        now: dt.datetime | None = None,
    ):
        self.backend = backend
        self.store = store
        self.cloud = cloud
        self.cloud_public_key = cloud_public_key
        self.now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)

    def recover(self, *, reason: str) -> dict[str, str]:
        active = self.store.active()
        required = {
            "device_id",
            "tenant_id",
            "line_id",
            "node_name",
            "identity_fingerprint",
            "runtime_credential",
            "runtime_credential_hash",
            "credential_expires_at",
        }
        if not active or any(
            not isinstance(active.get(field), str) or not active[field] for field in required
        ):
            raise RecoveryError("active device identity is unavailable")
        if (
            active.get("schema") != "drycontroller.device-identity.v1"
            or active["node_name"] != active["device_id"].lower()
            or hashlib.sha256(active["runtime_credential"].encode("utf-8")).hexdigest()
            != active["runtime_credential_hash"]
        ):
            raise RecoveryError("active device identity is invalid")
        try:
            expires_at = parse_timestamp(active["credential_expires_at"])
        except IdentityChallengeError as exc:
            raise RecoveryError("active device credential expiry is invalid") from exc
        if expires_at > self.now:
            raise RecoveryError("active device credential has not expired")
        try:
            public_key_spki = self.backend.public_key()
            fingerprint = hashlib.sha256(
                base64.b64decode(public_key_spki, validate=True)
            ).hexdigest()
        except (ValueError, RuntimeError) as exc:
            raise RecoveryError("TPM device identity is unavailable") from exc
        if fingerprint != active["identity_fingerprint"]:
            raise RecoveryError("TPM identity does not match the active device")

        try:
            candidate = self.store.create_recovery_candidate()
        except IdentityStateError as exc:
            raise RecoveryError(str(exc)) from exc
        request = {
            "request_id": candidate["request_id"],
            "new_identity_public_key_spki": public_key_spki,
            "new_identity_fingerprint": fingerprint,
            "expected_current_identity_fingerprint": fingerprint,
            "runtime_credential_hash": candidate["runtime_credential_hash"],
            "reason": reason,
        }
        response = self.cloud.post(
            f"/api/v1/device-identity/devices/{active['device_id']}/re-registration",
            request,
        )
        if response.get("state") != "active":
            challenge = {
                "schema": CHALLENGE_SCHEMA,
                "domain": CHALLENGE_DOMAIN,
                "operation": "re_register",
                "device_id": active["device_id"],
                "challenge_id": response.get("challenge_id"),
                "challenge_nonce": response.get("challenge_nonce"),
                "request_id": candidate["request_id"],
                "identity_fingerprint": fingerprint,
                "runtime_credential_hash": candidate["runtime_credential_hash"],
                "expires_at": response.get("challenge_expires_at"),
            }
            try:
                canonical_challenge(challenge)
                signature = self.backend.sign_identity_challenge(challenge)
            except (IdentityChallengeError, RuntimeError) as exc:
                raise RecoveryError("Cloud recovery challenge is invalid") from exc
            response = self.cloud.post(
                f"/api/v1/device-identity/devices/{active['device_id']}/re-registration/complete",
                {
                    "request_id": candidate["request_id"],
                    "runtime_credential_hash": candidate["runtime_credential_hash"],
                    "signature": signature,
                },
            )
        receipt = self._verified_receipt(response, active=active, candidate=candidate)
        try:
            recovered = self.store.promote_recovery(
                request_id=candidate["request_id"],
                runtime_credential_hash=candidate["runtime_credential_hash"],
                credential_expires_at=receipt["credential_expires_at"],
            )
        except IdentityStateError as exc:
            raise RecoveryError("credential recovery could not be promoted") from exc
        return {
            "status": "active",
            "device_id": recovered["device_id"],
            "credential_expires_at": recovered["credential_expires_at"],
        }

    def _verified_receipt(
        self,
        response: dict[str, Any],
        *,
        active: dict[str, Any],
        candidate: dict[str, Any],
    ) -> dict[str, str]:
        receipt = response.get("activation_receipt")
        signature_text = response.get("activation_receipt_signature")
        if not isinstance(receipt, dict) or not isinstance(signature_text, str):
            raise RecoveryError("Cloud recovery receipt is missing")
        try:
            signature = base64.b64decode(signature_text, validate=True)
            self.cloud_public_key.verify(signature, canonical_cloud_proof(receipt))
            expiry = parse_timestamp(receipt["credential_expires_at"])
        except (InvalidSignature, ValueError, KeyError, IdentityChallengeError):
            raise RecoveryError("Cloud recovery receipt is invalid") from None
        expected = {
            "schema": ACTIVATION_RECEIPT_SCHEMA,
            "domain": CLOUD_PROOF_DOMAIN,
            "operation": "activate",
            "state": "active",
            "request_id": candidate["request_id"],
            "device_id": active["device_id"],
            "tenant_id": active["tenant_id"],
            "line_id": active["line_id"],
            "node_name": active["node_name"],
            "identity_fingerprint": active["identity_fingerprint"],
            "runtime_credential_hash": candidate["runtime_credential_hash"],
            "credential_expires_at": receipt["credential_expires_at"],
        }
        if receipt != expected or response.get("state") != "active":
            raise RecoveryError("Cloud recovery receipt does not match the active device")
        if expiry <= self.now or expiry > self.now + dt.timedelta(hours=24, minutes=1):
            raise RecoveryError("Cloud recovery credential expiry is invalid")
        return receipt


def _load_cloud_public_key(path: Path) -> Ed25519PublicKey:
    try:
        metadata = path.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > 1024
            or metadata.st_mode & 0o022
            or (os.geteuid() == 0 and metadata.st_uid != 0)
        ):
            raise ValueError("invalid Cloud trust root metadata")
        raw = base64.b64decode(path.read_text(encoding="ascii").strip(), validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RecoveryError("Cloud identity trust root is unavailable") from exc


def recover_from_environment(
    *,
    cloud_url: str,
    username: str,
    password: str,
    reason: str,
) -> dict[str, str]:
    if os.geteuid() != 0 and os.getenv("DRYCONTROLLER_IDENTITY_ALLOW_NON_ROOT_TEST") != "1":
        raise RecoveryError("credential recovery must run as root")
    private_dir = Path(
        os.getenv("DRYCONTROLLER_IDENTITY_PRIVATE_DIR", "/var/lib/drycontroller-identity")
    )
    backend = load_backend(mode="tpm", key_path=private_dir / "identity-key.pem")
    store = IdentityStateStore(
        private_dir=private_dir,
        active_device_path=Path(
            os.getenv("DRYCONTROLLER_DEVICE_IDENTITY_PATH", "/var/lib/drycontroller/device.json")
        ),
    )
    cloud_public_key = _load_cloud_public_key(
        Path(
            os.getenv(
                "DRYCONTROLLER_IDENTITY_CLOUD_PUBLIC_KEY_FILE",
                "/etc/drycontroller/trust/device-identity-cloud.pub",
            )
        )
    )
    cloud = UrlLibAdminCloudClient(cloud_url, username=username, password=password)
    return CredentialRecovery(
        backend=backend,
        store=store,
        cloud=cloud,
        cloud_public_key=cloud_public_key,
    ).recover(reason=reason)


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover one expired Edge device credential")
    parser.add_argument("--cloud-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--reason", default="controlled active-device credential recovery")
    args = parser.parse_args()
    try:
        password = getpass.getpass("Cloud administrator password: ")
        result = recover_from_environment(password=password, **vars(args))
    except (RecoveryError, IdentityStateError, RuntimeError) as exc:
        print(f"device credential recovery failed: {exc}")
        return 1
    print(
        f"device credential active: device_id={result['device_id']} "
        f"expires={result['credential_expires_at']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
