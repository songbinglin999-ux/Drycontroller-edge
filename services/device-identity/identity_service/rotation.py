from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Protocol
import urllib.error
import urllib.request

from .backend import DeviceIdentityBackend, load_backend
from .contract import CHALLENGE_DOMAIN, CHALLENGE_SCHEMA, canonical_challenge
from .state import IdentityStateError, IdentityStateStore


class RotationError(RuntimeError):
    pass


_ALARM_SCHEMA = "drycontroller.device-identity-alarm.v1"
_ALARM_EVENT_FIELDS = {"schema", "event_id", "episode_id", "state", "device_id", "occurred_at"}


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
        mode="tpm",
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


def _record_rotation_alarm(*, state: str) -> None:
    device_path = Path(os.getenv("DRYCONTROLLER_DEVICE_IDENTITY_PATH", "/var/lib/drycontroller/device.json"))
    try:
        active = json.loads(device_path.read_text(encoding="utf-8"))
        device_id = active["device_id"]
        if not isinstance(device_id, str) or not device_id:
            return
        alarm_dir = device_path.with_name("device-identity-alarms")
        alarm_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(alarm_dir, 0o700)
        parent_fd = os.open(alarm_dir.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        prior = None
        for prior_path in sorted(alarm_dir.glob("*.json"), reverse=True):
            candidate = json.loads(prior_path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict) and set(candidate) == _ALARM_EVENT_FIELDS:
                prior = candidate
                break
        if state == "resolved" and (not prior or prior.get("state") != "firing"):
            return
        episode_id = (
            str(prior["episode_id"])
            if prior and prior.get("state") == "firing"
            else secrets.token_hex(16)
        )
        event_id = secrets.token_hex(16)
        payload = {
            "schema": _ALARM_SCHEMA,
            "event_id": event_id,
            "episode_id": episode_id,
            "state": state,
            "device_id": device_id,
            "occurred_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        event_path = alarm_dir / f"{time.time_ns():020d}-{event_id}.json"
        temporary = alarm_dir / f".{event_path.name}.{os.getpid()}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, event_path)
            os.chmod(event_path, 0o600)
            directory_fd = os.open(alarm_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            temporary.unlink(missing_ok=True)
            event_path.unlink(missing_ok=True)
            raise
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return


def main() -> int:
    try:
        result = rotate_from_environment()
    except (RotationError, IdentityStateError, RuntimeError) as exc:
        _record_rotation_alarm(state="firing")
        print(f"device credential rotation failed: {exc}")
        return 1
    _record_rotation_alarm(state="resolved")
    print(f"device credential active: device_id={result['device_id']} expires={result['credential_expires_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
