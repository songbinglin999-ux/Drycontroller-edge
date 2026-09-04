from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from identity_service.backend import FileTestIdentityBackend
from identity_service.contract import (
    ACTIVATION_RECEIPT_SCHEMA,
    CLOUD_PROOF_DOMAIN,
    canonical_cloud_proof,
    format_timestamp,
)
from identity_service.recovery import CredentialRecovery, RecoveryError
from identity_service.state import IdentityStateStore


NOW = dt.datetime(2026, 9, 4, 2, 0, tzinfo=dt.timezone.utc)
CLOUD_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


def _components(tmp_path: Path, *, expires_at: dt.datetime | None = None):
    private_dir = tmp_path / "private"
    active_path = tmp_path / "runtime" / "device.json"
    backend = FileTestIdentityBackend(private_dir / "identity-key.pem")
    public_key = backend.public_key()
    fingerprint = hashlib.sha256(base64.b64decode(public_key, validate=True)).hexdigest()
    active_path.parent.mkdir(mode=0o700)
    active_path.write_text(
        json.dumps(
            {
                "schema": "drycontroller.device-identity.v1",
                "request_id": "01" * 16,
                "device_id": "EDGE-ABCDEF123456",
                "tenant_id": "default",
                "line_id": "factory-cleanroom",
                "node_name": "edge-abcdef123456",
                "identity_fingerprint": fingerprint,
                "runtime_credential": "expired-secret",
                "runtime_credential_hash": hashlib.sha256(b"expired-secret").hexdigest(),
                "credential_expires_at": format_timestamp(
                    expires_at or (NOW - dt.timedelta(minutes=1))
                ),
            }
        ),
        encoding="utf-8",
    )
    active_path.chmod(0o600)
    store = IdentityStateStore(private_dir=private_dir, active_device_path=active_path)
    return backend, store, active_path


class FakeCloud:
    def __init__(
        self,
        *,
        active: dict,
        signer: Ed25519PrivateKey = CLOUD_KEY,
        already_active: bool = False,
    ):
        self.active = active
        self.signer = signer
        self.already_active = already_active
        self.calls: list[tuple[str, dict]] = []

    def post(self, path: str, payload: dict) -> dict:
        self.calls.append((path, payload))
        if path.endswith("/re-registration") and not self.already_active:
            return {
                "state": "pending",
                "challenge_id": "00000000-0000-4000-8000-000000000099",
                "challenge_nonce": base64.b64encode(b"r" * 32).decode("ascii"),
                "challenge_expires_at": format_timestamp(NOW + dt.timedelta(seconds=50)),
            }
        candidate_hash = payload["runtime_credential_hash"]
        receipt = {
            "schema": ACTIVATION_RECEIPT_SCHEMA,
            "domain": CLOUD_PROOF_DOMAIN,
            "operation": "activate",
            "state": "active",
            "request_id": payload["request_id"],
            "device_id": self.active["device_id"],
            "tenant_id": self.active["tenant_id"],
            "line_id": self.active["line_id"],
            "node_name": self.active["node_name"],
            "identity_fingerprint": self.active["identity_fingerprint"],
            "runtime_credential_hash": candidate_hash,
            "credential_expires_at": format_timestamp(NOW + dt.timedelta(hours=24)),
        }
        return {
            "state": "active",
            "activation_receipt": receipt,
            "activation_receipt_signature": base64.b64encode(
                self.signer.sign(canonical_cloud_proof(receipt))
            ).decode("ascii"),
        }


def test_expired_credential_recovers_only_after_tpm_challenge_and_signed_receipt(tmp_path):
    backend, store, active_path = _components(tmp_path)
    before = store.active()
    cloud = FakeCloud(active=before)

    result = CredentialRecovery(
        backend=backend,
        store=store,
        cloud=cloud,
        cloud_public_key=CLOUD_KEY.public_key(),
        now=NOW,
    ).recover(reason="operator-approved staging recovery")

    recovered = json.loads(active_path.read_text(encoding="utf-8"))
    assert result == {
        "status": "active",
        "device_id": before["device_id"],
        "credential_expires_at": format_timestamp(NOW + dt.timedelta(hours=24)),
    }
    assert recovered["runtime_credential_hash"] != before["runtime_credential_hash"]
    assert recovered["identity_fingerprint"] == before["identity_fingerprint"]
    assert store.recovery_candidate() is None
    assert [path.rsplit("/", 1)[-1] for path, _payload in cloud.calls] == [
        "re-registration",
        "complete",
    ]
    assert cloud.calls[0][1]["expected_current_identity_fingerprint"] == before[
        "identity_fingerprint"
    ]
    assert "signature" in cloud.calls[1][1]


def test_retry_promotes_same_candidate_after_cloud_completion_response_was_lost(tmp_path):
    backend, store, active_path = _components(tmp_path)
    before = store.active()
    candidate = store.create_recovery_candidate()
    cloud = FakeCloud(active=before, already_active=True)

    CredentialRecovery(
        backend=backend,
        store=store,
        cloud=cloud,
        cloud_public_key=CLOUD_KEY.public_key(),
        now=NOW,
    ).recover(reason="resume after response loss")

    recovered = json.loads(active_path.read_text(encoding="utf-8"))
    assert recovered["runtime_credential_hash"] == candidate["runtime_credential_hash"]
    assert len(cloud.calls) == 1
    assert store.recovery_candidate() is None


def test_nonexpired_credential_is_not_reissued(tmp_path):
    backend, store, _active_path = _components(
        tmp_path,
        expires_at=NOW + dt.timedelta(hours=1),
    )
    cloud = FakeCloud(active=store.active())

    with pytest.raises(RecoveryError, match="has not expired"):
        CredentialRecovery(
            backend=backend,
            store=store,
            cloud=cloud,
            cloud_public_key=CLOUD_KEY.public_key(),
            now=NOW,
        ).recover(reason="must not run")

    assert cloud.calls == []
    assert store.recovery_candidate() is None


def test_corrupt_active_credential_stops_before_cloud_or_candidate_mutation(tmp_path):
    backend, store, active_path = _components(tmp_path)
    active = json.loads(active_path.read_text(encoding="utf-8"))
    active["runtime_credential_hash"] = "00" * 32
    active_path.write_text(json.dumps(active), encoding="utf-8")
    cloud = FakeCloud(active=active)

    with pytest.raises(RecoveryError, match="identity is invalid"):
        CredentialRecovery(
            backend=backend,
            store=store,
            cloud=cloud,
            cloud_public_key=CLOUD_KEY.public_key(),
            now=NOW,
        ).recover(reason="must not run")

    assert cloud.calls == []
    assert store.recovery_candidate() is None


def test_tpm_mismatch_stops_before_cloud_or_candidate_mutation(tmp_path):
    _backend, store, _active_path = _components(tmp_path)
    replacement = FileTestIdentityBackend(tmp_path / "replacement" / "identity-key.pem")
    cloud = FakeCloud(active=store.active())

    with pytest.raises(RecoveryError, match="does not match"):
        CredentialRecovery(
            backend=replacement,
            store=store,
            cloud=cloud,
            cloud_public_key=CLOUD_KEY.public_key(),
            now=NOW,
        ).recover(reason="must not run")

    assert cloud.calls == []
    assert store.recovery_candidate() is None


def test_forged_cloud_receipt_does_not_replace_local_credential(tmp_path):
    backend, store, active_path = _components(tmp_path)
    before = active_path.read_bytes()

    with pytest.raises(RecoveryError, match="receipt is invalid"):
        CredentialRecovery(
            backend=backend,
            store=store,
            cloud=FakeCloud(active=store.active(), signer=Ed25519PrivateKey.generate()),
            cloud_public_key=CLOUD_KEY.public_key(),
            now=NOW,
        ).recover(reason="operator-approved staging recovery")

    assert active_path.read_bytes() == before
    assert store.recovery_candidate() is not None
