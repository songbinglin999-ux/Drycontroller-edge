from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

from .backend import DeviceIdentityBackend, load_backend
from .contract import IdentityChallengeError, canonical_challenge, parse_timestamp
from .state import IdentityStateStore


class HostAuthorizationError(RuntimeError):
    pass


_MAX_INPUT_BYTES = 16 * 1024


def sign_host_release_challenge(
    challenge: object,
    *,
    backend: DeviceIdentityBackend,
    active: dict[str, Any],
    now: dt.datetime | None = None,
) -> dict[str, str]:
    if not isinstance(challenge, dict) or not all(isinstance(key, str) for key in challenge):
        raise HostAuthorizationError("host release identity challenge is invalid")
    if challenge.get("operation") != "host_release":
        raise HostAuthorizationError("host release identity operation is invalid")
    if (
        challenge.get("device_id") != active.get("device_id")
        or challenge.get("tenant_id") != active.get("tenant_id")
        or challenge.get("line_id") != active.get("line_id")
        or challenge.get("node_name") != active.get("node_name")
        or challenge.get("identity_fingerprint") != active.get("identity_fingerprint")
        or challenge.get("runtime_credential_hash") != active.get("runtime_credential_hash")
    ):
        raise HostAuthorizationError("host release identity binding mismatch")
    try:
        canonical_challenge(challenge)
        expires_at = parse_timestamp(str(challenge["expires_at"]))
    except (IdentityChallengeError, KeyError, TypeError, ValueError) as exc:
        raise HostAuthorizationError("host release identity challenge is invalid") from exc
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    current = current.astimezone(dt.timezone.utc)
    if expires_at <= current or expires_at > current + dt.timedelta(seconds=60):
        raise HostAuthorizationError("host release identity challenge expired")
    try:
        signature = backend.sign_identity_challenge(challenge)
    except RuntimeError as exc:
        raise HostAuthorizationError("TPM host release authorization failed") from exc
    return {
        "challenge_id": str(challenge["challenge_id"]),
        "signature": signature,
    }


def sign_from_environment(challenge: object) -> dict[str, str]:
    if os.geteuid() != 0 and os.getenv("DRYCONTROLLER_IDENTITY_ALLOW_NON_ROOT_TEST") != "1":
        raise HostAuthorizationError("host release identity signing must run as root")
    private_dir = Path(os.getenv("DRYCONTROLLER_IDENTITY_PRIVATE_DIR", "/var/lib/drycontroller-identity"))
    store = IdentityStateStore(
        private_dir=private_dir,
        active_device_path=Path(
            os.getenv("DRYCONTROLLER_DEVICE_IDENTITY_PATH", "/var/lib/drycontroller/device.json")
        ),
    )
    active = store.active()
    if not active:
        raise HostAuthorizationError("active device identity is unavailable")
    backend = load_backend(mode="tpm", key_path=private_dir / "identity-key.pem")
    return sign_host_release_challenge(challenge, backend=backend, active=active)


def main() -> int:
    raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
    if len(raw) > _MAX_INPUT_BYTES:
        sys.stderr.write("host release identity challenge is too large\n")
        return 1
    try:
        challenge = json.loads(raw)
        result = sign_from_environment(challenge)
    except (UnicodeDecodeError, json.JSONDecodeError, HostAuthorizationError, RuntimeError) as exc:
        sys.stderr.write(f"host release identity authorization failed: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
