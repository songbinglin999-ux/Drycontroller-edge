import base64
import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient

from identity_service.api import create_app
from identity_service.backend import FileTestIdentityBackend, load_backend
from identity_service.contract import (
    ACTIVATION_RECEIPT_SCHEMA,
    BOOTSTRAP_GRANT_SCHEMA,
    CHALLENGE_DOMAIN,
    CHALLENGE_SCHEMA,
    CLOUD_PROOF_DOMAIN,
    canonical_challenge,
    canonical_cloud_proof,
    format_timestamp,
)
from identity_service.rotation import CredentialRotator, RotationError
from identity_service.state import IdentityStateStore


_CLOUD_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


def _signed_proof(payload):
    return base64.b64encode(_CLOUD_KEY.sign(canonical_cloud_proof(payload))).decode("ascii")


def _components(tmp_path):
    private_dir = tmp_path / "private"
    active_path = tmp_path / "runtime" / "device.json"
    backend = FileTestIdentityBackend(private_dir / "identity-key.pem")
    store = IdentityStateStore(private_dir=private_dir, active_device_path=active_path)
    return backend, store, active_path


def _activate_store(backend, store):
    state = store.initialize()
    device_id = "EDGE-ABCDEF123456"
    candidate = store.create_candidate(request_id=state["request_id"], device_id=device_id)
    active = store.activate(
        request_id=state["request_id"],
        device_id=device_id,
        tenant_id="tenant-a",
        line_id="LINE-A",
        node_name=device_id.lower(),
        identity_fingerprint=backend.health()["identity_fingerprint"],
        runtime_credential_hash=candidate["runtime_credential_hash"],
        credential_expires_at=format_timestamp(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)),
    )
    return active


def test_canonical_challenge_matches_the_frozen_cloud_vector():
    challenge = {
        "challenge_id": "00000000-0000-4000-8000-000000000001",
        "challenge_nonce": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
        "device_id": "EDGE-0123ABCD",
        "domain": CHALLENGE_DOMAIN,
        "expires_at": "2026-08-13T01:02:03Z",
        "identity_fingerprint": "21af110aa3ea3b9705cc6374830ce5bda521d901c5215f88604cc78385845dc0",
        "operation": "activate",
        "request_id": "0123456789abcdef0123456789abcdef",
        "runtime_credential_hash": "ab" * 32,
        "schema": CHALLENGE_SCHEMA,
    }

    assert canonical_challenge(challenge) == (
        b'{"challenge_id":"00000000-0000-4000-8000-000000000001",'
        b'"challenge_nonce":"AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",'
        b'"device_id":"EDGE-0123ABCD","domain":"drycontroller.device-identity",'
        b'"expires_at":"2026-08-13T01:02:03Z",'
        b'"identity_fingerprint":"21af110aa3ea3b9705cc6374830ce5bda521d901c5215f88604cc78385845dc0",'
        b'"operation":"activate","request_id":"0123456789abcdef0123456789abcdef",'
        b'"runtime_credential_hash":"abababababababababababababababababababababababababababababababab",'
        b'"schema":"drycontroller.device-identity-challenge.v1"}'
    )


def test_file_backend_is_explicit_and_key_is_stable_root_only(tmp_path):
    key_path = tmp_path / "identity" / "key.pem"
    with pytest.raises(RuntimeError):
        load_backend(mode="", key_path=key_path)
    with pytest.raises(RuntimeError):
        load_backend(mode="tpm", key_path=key_path)

    first = load_backend(mode="file-test", key_path=key_path)
    second = load_backend(mode="file-test", key_path=key_path)
    assert first.public_key() == second.public_key()
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert key_path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_factory_activation_persists_candidate_and_promotes_only_after_cloud_confirmation(tmp_path):
    backend, store, active_path = _components(tmp_path)
    app = create_app(backend=backend, store=store, cloud_public_key=_CLOUD_KEY.public_key())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://factory-edge") as client:
        identity_response = await client.get("/factory-identity")
        assert identity_response.status_code == 200
        identity = identity_response.json()
        device_id = "EDGE-ABCDEF123456"
        bootstrap_token = "bootstrap-token-kept-only-in-memory"
        bootstrap_expiry = format_timestamp(dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=14))
        grant = {
            "schema": BOOTSTRAP_GRANT_SCHEMA,
            "domain": CLOUD_PROOF_DOMAIN,
            "operation": "bootstrap",
            "request_id": identity["request_id"],
            "device_id": device_id,
            "tenant_id": "tenant-a",
            "line_id": "LINE-A",
            "node_name": device_id.lower(),
            "identity_fingerprint": identity["identity_fingerprint"],
            "bootstrap_token_hash": hashlib.sha256(bootstrap_token.encode()).hexdigest(),
            "expires_at": bootstrap_expiry,
        }
        bootstrap = {
            "request_id": identity["request_id"],
            "device_id": device_id,
            "bootstrap_token": bootstrap_token,
            "bootstrap_grant": grant,
            "bootstrap_grant_signature": _signed_proof(grant),
        }
        first_candidate = await client.post("/factory-identity/bootstrap", json=bootstrap)
        retry_candidate = await client.post("/factory-identity/bootstrap", json=bootstrap)
        assert first_candidate.status_code == 200
        assert retry_candidate.json() == first_candidate.json()
        candidate = first_candidate.json()
        assert store.candidate() is not None
        assert not active_path.exists()

        expiry = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) + dt.timedelta(seconds=50)
        challenge = {
            "schema": CHALLENGE_SCHEMA,
            "domain": CHALLENGE_DOMAIN,
            "operation": "activate",
            "device_id": device_id,
            "challenge_id": "00000000-0000-4000-8000-000000000002",
            "challenge_nonce": base64.b64encode(b"c" * 32).decode("ascii"),
            "request_id": identity["request_id"],
            "identity_fingerprint": identity["identity_fingerprint"],
            "runtime_credential_hash": candidate["runtime_credential_hash"],
            "expires_at": format_timestamp(expiry),
        }
        signed = await client.post("/factory-identity/challenge", json=challenge)
        assert signed.status_code == 200, signed.text
        signature = base64.b64decode(signed.json()["signature"], validate=True)
        public_key = serialization.load_der_public_key(
            base64.b64decode(identity["identity_public_key_spki"], validate=True)
        )
        assert isinstance(public_key, ec.EllipticCurvePublicKey)
        public_key.verify(signature, canonical_challenge(challenge), ec.ECDSA(hashes.SHA256()))

        credential_expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)
        receipt = {
            "schema": ACTIVATION_RECEIPT_SCHEMA,
            "domain": CLOUD_PROOF_DOMAIN,
            "operation": "activate",
            "state": "active",
            "request_id": identity["request_id"],
            "device_id": device_id,
            "tenant_id": "tenant-a",
            "line_id": "LINE-A",
            "node_name": device_id.lower(),
            "identity_fingerprint": identity["identity_fingerprint"],
            "runtime_credential_hash": candidate["runtime_credential_hash"],
            "credential_expires_at": format_timestamp(credential_expiry),
        }
        confirmation = {
            "activation_receipt": receipt,
            "activation_receipt_signature": _signed_proof(receipt),
        }
        confirmed = await client.post("/factory-identity/confirm", json=confirmation)
        assert confirmed.status_code == 200, confirmed.text
        assert (await client.get("/factory-identity")).status_code == 409

    restarted_app = create_app(backend=backend, store=store, cloud_public_key=_CLOUD_KEY.public_key())
    async with AsyncClient(transport=ASGITransport(app=restarted_app), base_url="http://factory-edge") as client:
        status_response = await client.get("/factory-identity/status")
        assert status_response.status_code == 200
        assert status_response.json()["device_id"] == device_id
        closed = await client.post("/factory-identity/close", json=confirmation)
        assert closed.status_code == 200
        assert (await client.get("/factory-identity/status")).status_code == 410
        shutdown = await client.post("/factory-identity/shutdown", json=confirmation)
        assert shutdown.status_code == 200

    active = json.loads(active_path.read_text(encoding="utf-8"))
    assert active["device_id"] == device_id
    assert hashlib.sha256(active["runtime_credential"].encode()).hexdigest() == candidate["runtime_credential_hash"]
    assert active_path.stat().st_mode & 0o777 == 0o600
    assert store.candidate() is None
    assert json.loads(store.factory_closed_path.read_text(encoding="utf-8"))["device_id"] == device_id
    persisted = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in store.private_dir.iterdir())
    assert bootstrap_token not in persisted
    assert active["runtime_credential"] not in persisted

    restarted_backend = FileTestIdentityBackend(store.private_dir / "identity-key.pem")
    assert restarted_backend.public_key() == backend.public_key()
    assert store.initialize() == {"status": "registered", "device_id": device_id}


def test_factory_service_stays_available_until_the_close_marker_is_committed():
    unit = (Path(__file__).resolve().parents[3] / "infra/bootstrap/drycontroller-factory-identity.service").read_text()
    assert "ConditionPathExists=!/var/lib/drycontroller-identity/factory-closed.json" in unit
    assert "ConditionPathExists=!/var/lib/drycontroller/device.json" not in unit


@pytest.mark.asyncio
async def test_factory_identity_rejects_challenge_that_does_not_match_candidate(tmp_path):
    backend, store, _active_path = _components(tmp_path)
    app = create_app(backend=backend, store=store, cloud_public_key=_CLOUD_KEY.public_key())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://factory-edge") as client:
        identity = (await client.get("/factory-identity")).json()
        bootstrap_token = "bootstrap-token-kept-only-in-memory"
        grant = {
            "schema": BOOTSTRAP_GRANT_SCHEMA,
            "domain": CLOUD_PROOF_DOMAIN,
            "operation": "bootstrap",
            "request_id": identity["request_id"],
            "device_id": "EDGE-ABCDEF123456",
            "tenant_id": "tenant-a",
            "line_id": "LINE-A",
            "node_name": "edge-abcdef123456",
            "identity_fingerprint": identity["identity_fingerprint"],
            "bootstrap_token_hash": hashlib.sha256(bootstrap_token.encode()).hexdigest(),
            "expires_at": format_timestamp(dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=14)),
        }
        await client.post(
            "/factory-identity/bootstrap",
            json={
                "request_id": identity["request_id"],
                "device_id": "EDGE-ABCDEF123456",
                "bootstrap_token": bootstrap_token,
                "bootstrap_grant": grant,
                "bootstrap_grant_signature": _signed_proof(grant),
            },
        )
        rejected = await client.post(
            "/factory-identity/challenge",
            json={
                "schema": CHALLENGE_SCHEMA,
                "domain": CHALLENGE_DOMAIN,
                "operation": "activate",
                "device_id": "EDGE-000000000000",
                "challenge_id": "00000000-0000-4000-8000-000000000002",
                "challenge_nonce": base64.b64encode(b"c" * 32).decode("ascii"),
                "request_id": identity["request_id"],
                "identity_fingerprint": identity["identity_fingerprint"],
                "runtime_credential_hash": "ab" * 32,
                "expires_at": format_timestamp(dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=50)),
            },
        )
    assert rejected.status_code == 409


@pytest.mark.asyncio
async def test_factory_identity_rejects_forged_local_bootstrap_and_confirmation(tmp_path):
    backend, store, active_path = _components(tmp_path)
    app = create_app(backend=backend, store=store, cloud_public_key=_CLOUD_KEY.public_key())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://factory-edge") as client:
        identity = (await client.get("/factory-identity")).json()
        forged = await client.post(
            "/factory-identity/bootstrap",
            json={
                "request_id": identity["request_id"],
                "device_id": "EDGE-ABCDEF123456",
                "bootstrap_token": "attacker-bootstrap-token-kept-only-in-memory",
                "bootstrap_grant": {
                    "schema": BOOTSTRAP_GRANT_SCHEMA,
                    "domain": CLOUD_PROOF_DOMAIN,
                    "operation": "bootstrap",
                    "request_id": identity["request_id"],
                    "device_id": "EDGE-ABCDEF123456",
                    "tenant_id": "attacker",
                    "line_id": "ATTACKER",
                    "node_name": "edge-abcdef123456",
                    "identity_fingerprint": identity["identity_fingerprint"],
                    "bootstrap_token_hash": "ab" * 32,
                    "expires_at": format_timestamp(dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=14)),
                },
                "bootstrap_grant_signature": base64.b64encode(b"forged").decode(),
            },
        )
        forged_confirmation = await client.post(
            "/factory-identity/confirm",
            json={
                "activation_receipt": {"schema": ACTIVATION_RECEIPT_SCHEMA},
                "activation_receipt_signature": base64.b64encode(b"forged").decode(),
            },
        )
    assert forged.status_code == 401
    assert forged_confirmation.status_code == 401
    assert store.candidate() is None
    assert not active_path.exists()


def test_credential_rotation_retries_response_loss_and_promotes_one_candidate(tmp_path):
    backend, store, active_path = _components(tmp_path)
    original = _activate_store(backend, store)

    class FakeCloud:
        def __init__(self):
            self.request = None
            self.active = False
            self.lose_completion_once = True
            self.expires_at = format_timestamp(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24))

        def post(self, path, payload):
            if path == "/api/v1/device-identity/credential-rotations":
                self.request = payload
                if self.active:
                    return {
                        "state": "active",
                        "device_id": payload["device_id"],
                        "request_id": payload["request_id"],
                        "runtime_credential_hash": payload["runtime_credential_hash"],
                        "credential_expires_at": self.expires_at,
                    }
                return {
                    "state": "pending",
                    "device_id": payload["device_id"],
                    "request_id": payload["request_id"],
                    "challenge_id": "00000000-0000-4000-8000-000000000003",
                    "challenge_nonce": base64.b64encode(b"r" * 32).decode("ascii"),
                    "challenge_expires_at": format_timestamp(
                        dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=50)
                    ),
                }
            assert self.request is not None
            signature = base64.b64decode(payload["signature"], validate=True)
            challenge = {
                "schema": CHALLENGE_SCHEMA,
                "domain": CHALLENGE_DOMAIN,
                "operation": "rotate",
                "device_id": self.request["device_id"],
                "challenge_id": "00000000-0000-4000-8000-000000000003",
                "challenge_nonce": base64.b64encode(b"r" * 32).decode("ascii"),
                "request_id": self.request["request_id"],
                "identity_fingerprint": self.request["identity_fingerprint"],
                "runtime_credential_hash": self.request["runtime_credential_hash"],
                "expires_at": format_timestamp(dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=50)),
            }
            # Use the exact expiry returned in the first response.
            challenge["expires_at"] = self.last_challenge_expiry
            public_key = serialization.load_der_public_key(base64.b64decode(backend.public_key(), validate=True))
            public_key.verify(signature, canonical_challenge(challenge), ec.ECDSA(hashes.SHA256()))
            self.active = True
            if self.lose_completion_once:
                self.lose_completion_once = False
                raise RotationError("simulated response loss")
            return {
                "state": "active",
                "device_id": self.request["device_id"],
                "request_id": self.request["request_id"],
                "runtime_credential_hash": self.request["runtime_credential_hash"],
                "credential_expires_at": self.expires_at,
            }

        @property
        def last_challenge_expiry(self):
            return self._challenge_expiry

        @last_challenge_expiry.setter
        def last_challenge_expiry(self, value):
            self._challenge_expiry = value

    cloud = FakeCloud()
    original_post = cloud.post

    def recording_post(path, payload):
        response = original_post(path, payload)
        if path == "/api/v1/device-identity/credential-rotations" and response.get("state") == "pending":
            cloud.last_challenge_expiry = response["challenge_expires_at"]
        return response

    cloud.post = recording_post
    rotator = CredentialRotator(backend=backend, store=store, cloud=cloud)
    with pytest.raises(RotationError, match="response loss"):
        rotator.rotate()

    candidate = store.rotation_candidate()
    assert candidate is not None
    assert json.loads(active_path.read_text(encoding="utf-8"))["runtime_credential_hash"] == original["runtime_credential_hash"]

    restarted_store = IdentityStateStore(private_dir=store.private_dir, active_device_path=active_path)
    result = CredentialRotator(backend=backend, store=restarted_store, cloud=cloud).rotate()
    rotated = json.loads(active_path.read_text(encoding="utf-8"))
    assert result["status"] == "active"
    assert rotated["runtime_credential_hash"] == candidate["runtime_credential_hash"]
    assert hashlib.sha256(rotated["runtime_credential"].encode()).hexdigest() == candidate["runtime_credential_hash"]
    assert restarted_store.rotation_candidate() is None
    assert active_path.stat().st_mode & 0o777 == 0o600


def test_credential_rotation_offline_keeps_active_identity_unchanged(tmp_path):
    backend, store, active_path = _components(tmp_path)
    original = _activate_store(backend, store)

    class OfflineCloud:
        def post(self, path, payload):
            raise RotationError("offline")

    with pytest.raises(RotationError, match="offline"):
        CredentialRotator(backend=backend, store=store, cloud=OfflineCloud()).rotate()
    active = json.loads(active_path.read_text(encoding="utf-8"))
    assert active["runtime_credential_hash"] == original["runtime_credential_hash"]
    assert store.rotation_candidate() is not None
