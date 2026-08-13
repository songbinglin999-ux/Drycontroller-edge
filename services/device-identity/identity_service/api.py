from __future__ import annotations

import datetime as dt
import base64
import hashlib
import os
import re
import signal
import stat
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import BackgroundTasks, FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from .backend import DeviceIdentityBackend, load_backend
from .contract import (
    ACTIVATION_RECEIPT_SCHEMA,
    BOOTSTRAP_GRANT_SCHEMA,
    CHALLENGE_DOMAIN,
    CHALLENGE_SCHEMA,
    CLOUD_PROOF_DOMAIN,
    IdentityChallengeError,
    as_utc,
    canonical_challenge,
    canonical_cloud_proof,
    parse_timestamp,
)
from .state import IdentityStateError, IdentityStateStore


_REQUEST_ID = re.compile(r"^[a-f0-9]{32}$")
_DEVICE_ID = re.compile(r"^EDGE-[A-F0-9]{12}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BootstrapRequest(StrictModel):
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    device_id: str = Field(pattern=r"^EDGE-[A-F0-9]{12}$")
    bootstrap_token: str = Field(min_length=32, max_length=256)
    bootstrap_grant: dict[str, str]
    bootstrap_grant_signature: str = Field(min_length=1, max_length=256)


class ChallengeRequest(StrictModel):
    schema_: str = Field(alias="schema")
    domain: str
    operation: str
    device_id: str
    challenge_id: str
    challenge_nonce: str
    request_id: str
    identity_fingerprint: str
    runtime_credential_hash: str
    expires_at: str


class ConfirmationRequest(StrictModel):
    activation_receipt: dict[str, str]
    activation_receipt_signature: str = Field(min_length=1, max_length=256)


def create_app(
    *,
    backend: DeviceIdentityBackend,
    store: IdentityStateStore,
    cloud_public_key: Ed25519PublicKey,
    stop_after_confirm: bool = False,
) -> FastAPI:
    app = FastAPI(title="DryController Factory Device Identity", docs_url=None, redoc_url=None)

    def _unregistered_state() -> dict:
        state = store.initialize()
        if state.get("status") == "registered":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="device_identity_already_active")
        return state

    @app.get("/health")
    async def health():
        return backend.health()

    @app.get("/factory-identity")
    async def factory_identity():
        state = _unregistered_state()
        public_key = backend.public_key()
        fingerprint = hashlib.sha256(base64.b64decode(public_key, validate=True)).hexdigest()
        return {
            "status": "unregistered",
            "request_id": state["request_id"],
            "first_boot_nonce": state["first_boot_nonce"],
            "identity_public_key_spki": public_key,
            "identity_fingerprint": fingerprint,
        }

    @app.post("/factory-identity/bootstrap")
    async def bootstrap(payload: BootstrapRequest):
        state = _unregistered_state()
        grant = payload.bootstrap_grant
        fingerprint = backend.health().get("identity_fingerprint")
        try:
            cloud_public_key.verify(
                base64.b64decode(payload.bootstrap_grant_signature, validate=True),
                canonical_cloud_proof(grant),
            )
            expiry = parse_timestamp(grant["expires_at"])
        except (InvalidSignature, ValueError, KeyError, IdentityChallengeError):
            raise HTTPException(status_code=401, detail="factory_identity_bootstrap_rejected") from None
        now = dt.datetime.now(dt.timezone.utc)
        if (
            payload.request_id != state.get("request_id")
            or grant.get("schema") != BOOTSTRAP_GRANT_SCHEMA
            or grant.get("domain") != CLOUD_PROOF_DOMAIN
            or grant.get("operation") != "bootstrap"
            or grant.get("request_id") != payload.request_id
            or grant.get("device_id") != payload.device_id
            or grant.get("node_name") != payload.device_id.lower()
            or grant.get("identity_fingerprint") != fingerprint
            or grant.get("bootstrap_token_hash")
            != hashlib.sha256(payload.bootstrap_token.encode("utf-8")).hexdigest()
            or expiry <= now
            or expiry > now + dt.timedelta(minutes=15)
        ):
            raise HTTPException(status_code=409, detail="factory_identity_request_mismatch")
        try:
            candidate = store.create_candidate(request_id=payload.request_id, device_id=payload.device_id)
        except IdentityStateError as exc:
            raise HTTPException(status_code=409, detail="factory_identity_candidate_conflict") from exc
        return {
            "request_id": payload.request_id,
            "device_id": payload.device_id,
            "runtime_credential_hash": candidate["runtime_credential_hash"],
        }

    @app.post("/factory-identity/challenge")
    async def sign_challenge(payload: ChallengeRequest):
        _unregistered_state()
        challenge = payload.model_dump(by_alias=True)
        candidate = store.candidate()
        fingerprint = backend.health().get("identity_fingerprint")
        if (
            not candidate
            or challenge["schema"] != CHALLENGE_SCHEMA
            or challenge["domain"] != CHALLENGE_DOMAIN
            or challenge["operation"] != "activate"
            or challenge["request_id"] != candidate.get("request_id")
            or challenge["device_id"] != candidate.get("device_id")
            or challenge["runtime_credential_hash"] != candidate.get("runtime_credential_hash")
            or challenge["identity_fingerprint"] != fingerprint
        ):
            raise HTTPException(status_code=409, detail="factory_identity_challenge_mismatch")
        try:
            expiry = parse_timestamp(challenge["expires_at"])
            now = dt.datetime.now(dt.timezone.utc)
            if expiry <= now or expiry > now + dt.timedelta(seconds=60):
                raise IdentityChallengeError("challenge expiry is outside the allowed window")
            canonical_challenge(challenge)
            signature = backend.sign_identity_challenge(challenge)
        except IdentityChallengeError as exc:
            raise HTTPException(status_code=400, detail="factory_identity_challenge_rejected") from exc
        return {"challenge_id": challenge["challenge_id"], "signature": signature}

    @app.post("/factory-identity/confirm")
    async def confirm(payload: ConfirmationRequest):
        if store.factory_closed():
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="factory_identity_closed")
        receipt = _verified_receipt(payload)
        fingerprint = backend.health().get("identity_fingerprint")
        if (
            receipt.get("identity_fingerprint") != fingerprint
            or receipt.get("node_name") != str(receipt.get("device_id", "")).lower()
        ):
            raise HTTPException(status_code=409, detail="factory_identity_confirmation_rejected")
        try:
            expiry = parse_timestamp(receipt["credential_expires_at"])
            now = as_utc(dt.datetime.now(dt.timezone.utc))
            if expiry <= now or expiry > now + dt.timedelta(hours=24, minutes=1):
                raise IdentityStateError("credential already expired")
            active = store.activate(
                request_id=receipt["request_id"],
                device_id=receipt["device_id"],
                tenant_id=receipt["tenant_id"],
                line_id=receipt["line_id"],
                node_name=receipt["node_name"],
                identity_fingerprint=receipt["identity_fingerprint"],
                runtime_credential_hash=receipt["runtime_credential_hash"],
                credential_expires_at=receipt["credential_expires_at"],
            )
        except (IdentityChallengeError, IdentityStateError, KeyError) as exc:
            raise HTTPException(status_code=409, detail="factory_identity_confirmation_rejected") from exc
        return {"status": "active", "device_id": active["device_id"], "stop_factory_service": True}

    @app.get("/factory-identity/status")
    async def identity_status():
        if store.factory_closed():
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="factory_identity_closed")
        active = store.active()
        if not active:
            raise HTTPException(status_code=404, detail="device_identity_not_active")
        return {
            "status": "active",
            "request_id": active["request_id"],
            "device_id": active["device_id"],
            "identity_fingerprint": active["identity_fingerprint"],
            "runtime_credential_hash": active["runtime_credential_hash"],
            "credential_expires_at": active["credential_expires_at"],
        }

    @app.post("/factory-identity/close")
    async def close_factory_endpoint(payload: ConfirmationRequest):
        receipt = _verified_receipt(payload)
        active = store.active()
        if not active or any(
            active.get(field) != receipt.get(field)
            for field in (
                "request_id",
                "device_id",
                "identity_fingerprint",
                "runtime_credential_hash",
                "credential_expires_at",
            )
        ):
            raise HTTPException(status_code=409, detail="factory_identity_close_rejected")
        try:
            store.close_factory_endpoint()
        except IdentityStateError as exc:
            raise HTTPException(status_code=409, detail="factory_identity_close_rejected") from exc
        return {"status": "closed", "device_id": active["device_id"]}

    @app.post("/factory-identity/shutdown")
    async def shutdown_factory_service(payload: ConfirmationRequest, background_tasks: BackgroundTasks):
        receipt = _verified_receipt(payload)
        active = store.active()
        marker = store.factory_closed()
        if (
            not active
            or not marker
            or marker.get("request_id") != active.get("request_id")
            or marker.get("device_id") != active.get("device_id")
            or any(
                active.get(field) != receipt.get(field)
                for field in (
                    "request_id",
                    "device_id",
                    "identity_fingerprint",
                    "runtime_credential_hash",
                    "credential_expires_at",
                )
            )
        ):
            raise HTTPException(status_code=409, detail="factory_identity_shutdown_rejected")
        if stop_after_confirm:
            background_tasks.add_task(os.kill, os.getpid(), signal.SIGTERM)
        return {"status": "closed", "device_id": active["device_id"]}

    def _verified_receipt(payload: ConfirmationRequest) -> dict[str, str]:
        receipt = payload.activation_receipt
        try:
            cloud_public_key.verify(
                base64.b64decode(payload.activation_receipt_signature, validate=True),
                canonical_cloud_proof(receipt),
            )
        except (InvalidSignature, ValueError, IdentityChallengeError):
            raise HTTPException(status_code=401, detail="factory_identity_confirmation_rejected") from None
        if (
            receipt.get("schema") != ACTIVATION_RECEIPT_SCHEMA
            or receipt.get("domain") != CLOUD_PROOF_DOMAIN
            or receipt.get("operation") != "activate"
            or receipt.get("state") != "active"
        ):
            raise HTTPException(status_code=409, detail="factory_identity_confirmation_rejected")
        return receipt

    return app


def create_app_from_environment() -> FastAPI:
    if os.geteuid() != 0 and os.getenv("DRYCONTROLLER_IDENTITY_ALLOW_NON_ROOT_TEST") != "1":
        raise RuntimeError("device identity service must run as root")
    private_dir = Path(os.getenv("DRYCONTROLLER_IDENTITY_PRIVATE_DIR", "/var/lib/drycontroller-identity"))
    key_path = private_dir / "identity-key.pem"
    backend = load_backend(mode=os.getenv("DRYCONTROLLER_IDENTITY_BACKEND", ""), key_path=key_path)
    store = IdentityStateStore(
        private_dir=private_dir,
        active_device_path=Path(os.getenv("DRYCONTROLLER_DEVICE_IDENTITY_PATH", "/var/lib/drycontroller/device.json")),
    )
    cloud_public_key = _load_cloud_public_key(
        Path(
            os.getenv(
                "DRYCONTROLLER_IDENTITY_CLOUD_PUBLIC_KEY_FILE",
                "/etc/drycontroller/trust/device-identity-cloud.pub",
            )
        )
    )
    return create_app(
        backend=backend,
        store=store,
        cloud_public_key=cloud_public_key,
        stop_after_confirm=True,
    )


def _load_cloud_public_key(path: Path) -> Ed25519PublicKey:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o022:
            raise RuntimeError("Cloud identity trust key must be a non-writable regular file")
        if os.geteuid() == 0 and metadata.st_uid != 0:
            raise RuntimeError("Cloud identity trust key must be root-owned")
        raw = base64.b64decode(path.read_text(encoding="ascii").strip(), validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("Cloud identity trust key is invalid") from exc
