from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol
import urllib.error
import urllib.request

from .backend import DeviceIdentityBackend, load_backend
from .contract import CHALLENGE_DOMAIN, CHALLENGE_SCHEMA, canonical_challenge
from .state import IdentityStateError, IdentityStateStore


class RotationError(RuntimeError):
    pass


class IdentityCloudClient(Protocol):
    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class UrlLibIdentityCloudClient:
    def __init__(self, base_url: str, *, credential: str):
        if not base_url.startswith("https://"):
            raise RotationError("Cloud identity URL must use HTTPS")
        self.base_url = base_url.rstrip("/")
        if not credential:
            raise RotationError("active device credential is required")
        self.credential = credential

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Device-Credential": self.credential,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read(65_537)
        except (OSError, urllib.error.URLError) as exc:
            raise RotationError("Cloud credential rotation is unavailable") from exc
        if len(raw) > 65_536:
            raise RotationError("Cloud credential rotation response is too large")
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RotationError("Cloud credential rotation response is invalid") from exc
        if not isinstance(result, dict):
            raise RotationError("Cloud credential rotation response is invalid")
        return result


class CredentialRotator:
    def __init__(
        self,
        *,
        backend: DeviceIdentityBackend,
        store: IdentityStateStore,
        cloud: IdentityCloudClient,
    ):
        self.backend = backend
        self.store = store
        self.cloud = cloud

    def rotate(self) -> dict[str, str]:
        candidate = self.store.create_rotation_candidate()
        if candidate.get("already_promoted"):
            active = self.store.active()
            if not active:
                raise RotationError("active device identity is unavailable")
            return {
                "status": "active",
                "device_id": active["device_id"],
                "credential_expires_at": active["credential_expires_at"],
            }
        request = {
            "device_id": candidate["device_id"],
            "request_id": candidate["request_id"],
            "identity_fingerprint": candidate["identity_fingerprint"],
            "runtime_credential_hash": candidate["runtime_credential_hash"],
        }
        challenge = self.cloud.post("/api/v1/device-identity/credential-rotations", request)
        if challenge.get("state") == "active":
            return self._promote(candidate, challenge)
        signed = {
            "schema": CHALLENGE_SCHEMA,
            "domain": CHALLENGE_DOMAIN,
            "operation": "rotate",
            "device_id": candidate["device_id"],
            "challenge_id": challenge.get("challenge_id"),
            "challenge_nonce": challenge.get("challenge_nonce"),
            "request_id": candidate["request_id"],
            "identity_fingerprint": candidate["identity_fingerprint"],
            "runtime_credential_hash": candidate["runtime_credential_hash"],
            "expires_at": challenge.get("challenge_expires_at"),
        }
        try:
            canonical_challenge(signed)
        except (TypeError, ValueError) as exc:
            raise RotationError("Cloud identity challenge is invalid") from exc
        signature = self.backend.sign_identity_challenge(signed)
        completed = self.cloud.post(
            f"/api/v1/device-identity/credential-rotations/{candidate['request_id']}/complete",
            {
                "runtime_credential_hash": candidate["runtime_credential_hash"],
                "signature": signature,
            },
        )
        return self._promote(candidate, completed)

    def _promote(self, candidate: dict[str, Any], response: dict[str, Any]) -> dict[str, str]:
        if (
            response.get("state") != "active"
            or response.get("device_id") != candidate["device_id"]
            or response.get("request_id") != candidate["request_id"]
            or response.get("runtime_credential_hash") != candidate["runtime_credential_hash"]
            or not isinstance(response.get("credential_expires_at"), str)
        ):
            raise RotationError("Cloud credential rotation confirmation is invalid")
        try:
            active = self.store.promote_rotation(
                request_id=candidate["request_id"],
                runtime_credential_hash=candidate["runtime_credential_hash"],
                credential_expires_at=response["credential_expires_at"],
            )
        except IdentityStateError as exc:
            raise RotationError("credential rotation state could not be promoted") from exc
        return {
            "status": "active",
            "device_id": active["device_id"],
            "credential_expires_at": active["credential_expires_at"],
        }


def rotate_from_environment() -> dict[str, str]:
    if os.geteuid() != 0 and os.getenv("DRYCONTROLLER_IDENTITY_ALLOW_NON_ROOT_TEST") != "1":
        raise RotationError("credential rotation must run as root")
    private_dir = Path(os.getenv("DRYCONTROLLER_IDENTITY_PRIVATE_DIR", "/var/lib/drycontroller-identity"))
    backend = load_backend(
        mode=os.getenv("DRYCONTROLLER_IDENTITY_BACKEND", ""),
        key_path=private_dir / "identity-key.pem",
    )
    store = IdentityStateStore(
        private_dir=private_dir,
        active_device_path=Path(os.getenv("DRYCONTROLLER_DEVICE_IDENTITY_PATH", "/var/lib/drycontroller/device.json")),
    )
    active = store.active()
    if not active or not isinstance(active.get("runtime_credential"), str):
        raise RotationError("active device credential is unavailable")
    cloud_url = os.getenv("CLOUD_OPS_BASE_URL", "").strip()
    return CredentialRotator(
        backend=backend,
        store=store,
        cloud=UrlLibIdentityCloudClient(cloud_url, credential=active["runtime_credential"]),
    ).rotate()


def main() -> int:
    try:
        result = rotate_from_environment()
    except (RotationError, IdentityStateError, RuntimeError) as exc:
        print(f"device credential rotation failed: {exc}")
        return 1
    print(f"device credential active: device_id={result['device_id']} expires={result['credential_expires_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
