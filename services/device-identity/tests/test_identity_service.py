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
from identity_service import rotation as rotation_module
from identity_service.backend import FileTestIdentityBackend, TpmIdentityBackend, load_backend
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
from identity_service.tailnet import TailnetClient, TailnetJoinError


_CLOUD_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


def _signed_proof(payload):
    return base64.b64encode(_CLOUD_KEY.sign(canonical_cloud_proof(payload))).decode("ascii")


def _components(tmp_path):
    private_dir = tmp_path / "private"
    active_path = tmp_path / "runtime" / "device.json"
    backend = FileTestIdentityBackend(private_dir / "identity-key.pem")
    store = IdentityStateStore(private_dir=private_dir, active_device_path=active_path)
    return backend, store, active_path


def _challenge(identity_fingerprint):
    return {
        "schema": CHALLENGE_SCHEMA,
        "domain": CHALLENGE_DOMAIN,
        "operation": "rotate",
        "device_id": "EDGE-ABCDEF123456",
        "challenge_id": "00000000-0000-4000-8000-000000000004",
        "challenge_nonce": base64.b64encode(b"t" * 32).decode("ascii"),
        "request_id": "0123456789abcdef0123456789abcdef",
        "identity_fingerprint": identity_fingerprint,
        "runtime_credential_hash": "ab" * 32,
        "expires_at": "2026-08-13T01:02:03Z",
    }


class FakeTpmTools:
    def __init__(self, *, signer=None, fail_tool=None):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.signer = signer or self.key
        self.fail_tool = fail_tool
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        tool = Path(command[0]).name
        if tool == self.fail_tool:
            raise RuntimeError(f"{tool} failed")
        if tool == "tpm2_createprimary":
            context_path = Path(command[command.index("--key-context") + 1])
            public_path = Path(command[command.index("--output") + 1])
            context_path.write_bytes(b"fake same-TPM context")
            public_path.write_bytes(
                self.key.public_key().public_bytes(
                    encoding=serialization.Encoding.DER,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            )
        elif tool == "tpm2_sign":
            signature_path = Path(command[command.index("--signature") + 1])
            message_path = Path(command[-1])
            signature_path.write_bytes(
                self.signer.sign(message_path.read_bytes(), ec.ECDSA(hashes.SHA256()))
            )


class FakeTailnet:
    def __init__(self):
        self.online = False
        self.keys: list[str] = []
        self.expected_version = "1.98.10"
        self.login_server = "https://headscale.example.test"
        self.fail_after_join = False

    def status(
        self,
        *,
        node_name: str,
        expected_node_id: str | None = None,
        allow_unbound_node_id: bool = False,
    ):
        if not self.online:
            return {"ok": False, "reason": "tailscale_not_running", "detail": {}}
        if expected_node_id is not None and expected_node_id != "node-1":
            return {
                "ok": False,
                "reason": "tailscale_node_identity_mismatch",
                "detail": {"hostname": node_name, "node_id": "node-1"},
            }
        return {"ok": True, "reason": "ok", "detail": {"hostname": node_name, "node_id": "node-1"}}

    def join(self, *, node_name: str, auth_key: str):
        self.keys.append(auth_key)
        self.online = True
        if self.fail_after_join:
            self.fail_after_join = False
            raise TailnetJoinError("tailnet_join_failed")
        return self.status(node_name=node_name)

    def ensure_ssh(self, *, node_name: str, expected_node_id: str):
        return self.status(node_name=node_name, expected_node_id=expected_node_id)


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

    first = load_backend(mode="file-test", key_path=key_path)
    second = load_backend(mode="file-test", key_path=key_path)
    assert first.public_key() == second.public_key()
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert key_path.parent.stat().st_mode & 0o777 == 0o700


def test_tpm_backend_recreates_one_fixed_primary_and_returns_rb5a_wire_format(tmp_path):
    fake_tpm = FakeTpmTools()
    key_path = tmp_path / "must-not-exist.pem"
    first = load_backend(mode="tpm", key_path=key_path, run_command=fake_tpm)
    second = TpmIdentityBackend(run_command=fake_tpm)

    first_public = first.public_key()
    assert second.public_key() == first_public
    assert not key_path.exists()
    fingerprint = hashlib.sha256(base64.b64decode(first_public, validate=True)).hexdigest()
    assert first.health() == {"status": "ok", "identity_fingerprint": fingerprint}

    challenge = _challenge(fingerprint)
    signature = base64.b64decode(first.sign_identity_challenge(challenge), validate=True)
    public_key = serialization.load_der_public_key(base64.b64decode(first_public, validate=True))
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    public_key.verify(signature, canonical_challenge(challenge), ec.ECDSA(hashes.SHA256()))

    create_commands = [
        command for command in fake_tpm.commands if Path(command[0]).name == "tpm2_createprimary"
    ]
    assert create_commands
    for command in create_commands:
        assert command[0] == "/usr/bin/tpm2_createprimary"
        assert command[command.index("--tcti") + 1] == "device:/dev/tpmrm0"
        assert command[command.index("--hierarchy") + 1] == "o"
        assert command[command.index("--hash-algorithm") + 1] == "sha256"
        assert command[command.index("--key-algorithm") + 1] == "ecc256:ecdsa-sha256"
        assert command[command.index("--attributes") + 1] == (
            "fixedtpm|fixedparent|sensitivedataorigin|userwithauth|sign|noda"
        )
        assert "--private" not in command
        assert not Path(command[command.index("--key-context") + 1]).parent.exists()
    sign_command = next(command for command in fake_tpm.commands if Path(command[0]).name == "tpm2_sign")
    assert sign_command[sign_command.index("--format") + 1] == "plain"
    assert not any(Path(command[0]).name == "tpm2_evictcontrol" for command in fake_tpm.commands)


def test_tpm_backend_fails_closed_without_fallback_or_unverified_signature():
    unavailable = TpmIdentityBackend(run_command=FakeTpmTools(fail_tool="tpm2_createprimary"))
    assert unavailable.health() == {"status": "degraded", "reason": "tpm_identity_unavailable"}
    with pytest.raises(RuntimeError, match="TPM identity unavailable"):
        unavailable.public_key()

    wrong_signer = ec.generate_private_key(ec.SECP256R1())
    invalid = TpmIdentityBackend(run_command=FakeTpmTools(signer=wrong_signer))
    fingerprint = invalid.health()["identity_fingerprint"]
    with pytest.raises(RuntimeError, match="TPM identity signature verification failed"):
        invalid.sign_identity_challenge(_challenge(fingerprint))

    changed = TpmIdentityBackend(run_command=FakeTpmTools())
    with pytest.raises(RuntimeError, match="TPM identity fingerprint changed"):
        changed.sign_identity_challenge(_challenge("00" * 32))


def test_production_rotation_entrypoint_cannot_be_switched_to_file_key(monkeypatch):
    monkeypatch.setenv("DRYCONTROLLER_IDENTITY_ALLOW_NON_ROOT_TEST", "1")
    monkeypatch.setenv("DRYCONTROLLER_IDENTITY_BACKEND", "file-test")

    def capture_backend(*, mode, key_path):
        assert mode == "tpm"
        assert key_path.name == "identity-key.pem"
        raise RuntimeError("backend captured")

    monkeypatch.setattr(rotation_module, "load_backend", capture_backend)
    with pytest.raises(RuntimeError, match="backend captured"):
        rotation_module.rotate_from_environment()


def test_rotation_failure_and_recovery_append_root_only_alarm_events(tmp_path, monkeypatch):
    device_path = tmp_path / "device.json"
    device_path.write_text(json.dumps({"device_id": "EDGE-ABCDEF123456"}), encoding="utf-8")
    monkeypatch.setenv("DRYCONTROLLER_DEVICE_IDENTITY_PATH", str(device_path))

    def fail_rotation():
        raise RuntimeError("TPM unavailable")

    monkeypatch.setattr(rotation_module, "rotate_from_environment", fail_rotation)
    assert rotation_module.main() == 1
    alarm_dir = tmp_path / "device-identity-alarms"
    firing_path = next(alarm_dir.glob("*.json"))
    firing = json.loads(firing_path.read_text(encoding="utf-8"))
    assert firing["schema"] == "drycontroller.device-identity-alarm.v1"
    assert firing["state"] == "firing"
    assert firing["device_id"] == "EDGE-ABCDEF123456"
    assert alarm_dir.stat().st_mode & 0o777 == 0o700
    assert firing_path.stat().st_mode & 0o777 == 0o600

    monkeypatch.setattr(
        rotation_module,
        "rotate_from_environment",
        lambda: {
            "status": "active",
            "device_id": "EDGE-ABCDEF123456",
            "credential_expires_at": "2026-08-14T00:00:00Z",
        },
    )
    assert rotation_module.main() == 0
    event_paths = sorted(alarm_dir.glob("*.json"))
    assert len(event_paths) == 2
    resolved = json.loads(event_paths[1].read_text(encoding="utf-8"))
    assert resolved["state"] == "resolved"
    assert resolved["event_id"] != firing["event_id"]
    assert resolved["episode_id"] == firing["episode_id"]


def test_cloud_outage_is_reported_as_generic_credential_rotation_failure(tmp_path, monkeypatch, capsys):
    device_path = tmp_path / "device.json"
    device_path.write_text(json.dumps({"device_id": "EDGE-ABCDEF123456"}), encoding="utf-8")
    monkeypatch.setenv("DRYCONTROLLER_DEVICE_IDENTITY_PATH", str(device_path))
    monkeypatch.setattr(
        rotation_module,
        "rotate_from_environment",
        lambda: (_ for _ in ()).throw(RotationError("Cloud credential rotation is unavailable")),
    )

    assert rotation_module.main() == 1
    assert "Cloud credential rotation is unavailable" in capsys.readouterr().out
    event_path = next((tmp_path / "device-identity-alarms").glob("*.json"))
    event = json.loads(event_path.read_text(encoding="utf-8"))
    assert event["state"] == "firing"
    assert "reason" not in event


def test_production_identity_units_allow_only_the_kernel_tpm_resource_manager():
    infra = Path(__file__).resolve().parents[3] / "infra" / "bootstrap"
    for name in ("drycontroller-factory-identity.service", "drycontroller-device-credential-rotation.service"):
        unit = (infra / name).read_text(encoding="utf-8")
        assert "DevicePolicy=closed" in unit
        assert "DeviceAllow=/dev/tpmrm0 rw" in unit
    factory_unit = (infra / "drycontroller-factory-identity.service").read_text(encoding="utf-8")
    tailnet_source = (
        Path(__file__).resolve().parents[1] / "identity_service" / "tailnet.py"
    ).read_text(encoding="utf-8")
    installer = (infra / "install-edge-host.sh").read_text(encoding="utf-8")
    assert "Environment=DRYCONTROLLER_IDENTITY_BACKEND=tpm" in factory_unit
    assert "RuntimeDirectory=drycontroller-tailnet-join" in factory_unit
    assert "RuntimeDirectoryMode=0700" in factory_unit
    assert "ProtectSystem=strict" in factory_unit
    assert '["/usr/bin/systemctl", "start", TAILNET_EDGE_API_SOCKET]' in tailnet_source
    assert '["/usr/bin/systemctl", "enable"' not in tailnet_source
    assert "systemctl enable drycontroller-tailnet-edge-api.socket" in installer


@pytest.mark.asyncio
async def test_factory_activation_persists_candidate_and_promotes_only_after_cloud_confirmation(tmp_path):
    backend, store, active_path = _components(tmp_path)
    tailnet = FakeTailnet()
    app = create_app(
        backend=backend,
        store=store,
        cloud_public_key=_CLOUD_KEY.public_key(),
        tailnet=tailnet,
    )
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
        assert (await client.post("/factory-identity/tailnet/status", json=confirmation)).status_code == 409
        joined = await client.post(
            "/factory-identity/tailnet/join",
            json={**confirmation, "tailnet_auth_key": "hskey-auth-0123456789abcdef"},
        )
        assert joined.status_code == 200
        assert joined.json()["tailnet"]["hostname"] == device_id.lower()
        assert tailnet.keys == ["hskey-auth-0123456789abcdef"]
        assert (await client.post("/factory-identity/tailnet/status", json=confirmation)).status_code == 200
        assert store.active()["tailnet_node_id"] == "node-1"

    restarted_app = create_app(
        backend=backend,
        store=store,
        cloud_public_key=_CLOUD_KEY.public_key(),
        tailnet=tailnet,
    )
    async with AsyncClient(transport=ASGITransport(app=restarted_app), base_url="http://factory-edge") as client:
        status_response = await client.get("/factory-identity/status")
        assert status_response.status_code == 200
        assert status_response.json()["device_id"] == device_id
        closed = await client.post("/factory-identity/close", json=confirmation)
        assert closed.status_code == 200
        assert (await client.get("/factory-identity/status")).status_code == 410
        assert (await client.post("/factory-identity/tailnet/status", json=confirmation)).status_code == 410
        assert (
            await client.post(
                "/factory-identity/tailnet/join",
                json={**confirmation, "tailnet_auth_key": "hskey-auth-0123456789abcdef"},
            )
        ).status_code == 410
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


def test_tailnet_join_uses_root_only_file_argument_and_deletes_it(tmp_path):
    online = False
    commands: list[list[str]] = []
    key_paths: list[Path] = []
    auth_key = "hskey-auth-0123456789abcdef"

    def inspect_status(**kwargs):
        return {
            "ok": online,
            "reason": "ok" if online else "tailscale_not_running",
            "detail": {"hostname": kwargs["expected_hostname"], "node_id": "node-1"} if online else {},
        }

    def run_command(command):
        nonlocal online
        commands.append(command)
        if command[0] == "/usr/bin/systemctl":
            return 0
        key_argument = next(item for item in command if item.startswith("--auth-key=file:"))
        key_path = Path(key_argument.removeprefix("--auth-key=file:"))
        key_paths.append(key_path)
        assert key_path.read_text(encoding="ascii") == f"{auth_key}\n"
        assert key_path.stat().st_mode & 0o777 == 0o600
        online = True
        return 0

    client = TailnetClient(
        expected_version="1.98.10",
        login_server="https://headscale.example.test",
        temp_root=tmp_path / "join",
        run_command=run_command,
        inspect_status=inspect_status,
    )
    result = client.join(node_name="edge-abcdef123456", auth_key=auth_key)

    assert result["ok"] is True
    assert commands and all(auth_key not in argument for command in commands for argument in command)
    assert "--ssh" in commands[0]
    assert ["/usr/bin/systemctl", "start", "drycontroller-tailnet-edge-api.socket"] in commands
    assert key_paths and not key_paths[0].exists()
    assert (tmp_path / "join").stat().st_mode & 0o777 == 0o700


def test_tailnet_join_failure_deletes_key_and_never_persists_it(tmp_path):
    observed_key_path: Path | None = None

    def run_command(command):
        nonlocal observed_key_path
        key_argument = next(item for item in command if item.startswith("--auth-key=file:"))
        observed_key_path = Path(key_argument.removeprefix("--auth-key=file:"))
        return 1

    client = TailnetClient(
        expected_version="1.98.10",
        login_server="https://headscale.example.test",
        temp_root=tmp_path / "join",
        run_command=run_command,
        inspect_status=lambda **_kwargs: {"ok": False, "reason": "tailscale_not_running", "detail": {}},
    )

    with pytest.raises(TailnetJoinError, match="tailnet_auth_key_rejected"):
        client.join(
            node_name="edge-abcdef123456",
            auth_key="hskey-auth-0123456789abcdef",
        )
    assert observed_key_path is not None and not observed_key_path.exists()


def test_tailnet_bound_identity_enables_ssh_without_rejoining(tmp_path):
    ssh_enabled = False
    commands: list[list[str]] = []

    def inspect_status(**_kwargs):
        return {
            "ok": ssh_enabled,
            "reason": "ok" if ssh_enabled else "tailscale_ssh_disabled",
            "detail": {
                "hostname": "edge-abcdef123456",
                "node_id": "node-1",
                "ssh_enabled": ssh_enabled,
            },
        }

    def run_command(command):
        nonlocal ssh_enabled
        commands.append(command)
        if command[-2:] == ["set", "--ssh"]:
            ssh_enabled = True
        return 0

    client = TailnetClient(
        expected_version="1.98.10",
        login_server="https://headscale.example.test",
        temp_root=tmp_path / "join",
        run_command=run_command,
        inspect_status=inspect_status,
    )

    result = client.ensure_ssh(
        node_name="edge-abcdef123456", expected_node_id="node-1"
    )

    assert result["ok"] is True
    assert commands == [
        [
            "/usr/bin/systemctl",
            "is-active",
            "--quiet",
            "drycontroller-tailnet-edge-api.socket",
        ],
        [
            "/usr/bin/tailscale",
            "--socket=/var/run/tailscale/tailscaled.sock",
            "set",
            "--ssh",
        ],
        [
            "/usr/bin/systemctl",
            "is-active",
            "--quiet",
            "drycontroller-tailnet-edge-api.socket",
        ],
    ]


def test_tailnet_identity_mismatch_never_enables_ssh(tmp_path):
    commands: list[list[str]] = []
    client = TailnetClient(
        expected_version="1.98.10",
        login_server="https://headscale.example.test",
        temp_root=tmp_path / "join",
        run_command=lambda command: commands.append(command) or 0,
        inspect_status=lambda **_kwargs: {
            "ok": False,
            "reason": "tailscale_node_identity_mismatch",
            "detail": {"node_id": "other-node", "ssh_enabled": False},
        },
    )

    result = client.ensure_ssh(
        node_name="edge-abcdef123456", expected_node_id="node-1"
    )

    assert result["reason"] == "tailscale_node_identity_mismatch"
    assert commands == []


def test_tailnet_inactive_ingress_never_enables_ssh(tmp_path):
    commands: list[list[str]] = []
    client = TailnetClient(
        expected_version="1.98.10",
        login_server="https://headscale.example.test",
        temp_root=tmp_path / "join",
        run_command=lambda command: commands.append(command) or 1,
        inspect_status=lambda **_kwargs: {
            "ok": False,
            "reason": "tailscale_ssh_disabled",
            "detail": {"node_id": "node-1", "ssh_enabled": False},
        },
    )

    result = client.ensure_ssh(
        node_name="edge-abcdef123456", expected_node_id="node-1"
    )

    assert result["reason"] == "tailnet_edge_api_ingress_inactive"
    assert commands == [
        [
            "/usr/bin/systemctl",
            "is-active",
            "--quiet",
            "drycontroller-tailnet-edge-api.socket",
        ]
    ]


def test_tailnet_ssh_verification_failure_disables_ssh(tmp_path):
    ssh_enabled = False
    inspect_count = 0
    commands: list[list[str]] = []

    def inspect_status(**_kwargs):
        nonlocal inspect_count
        inspect_count += 1
        reason = "tailscale_ssh_disabled" if inspect_count == 1 else "tailscale_node_identity_mismatch"
        return {
            "ok": False,
            "reason": reason,
            "detail": {"node_id": "node-1", "ssh_enabled": ssh_enabled},
        }

    def run_command(command):
        nonlocal ssh_enabled
        commands.append(command)
        if command[-2:] == ["set", "--ssh"]:
            ssh_enabled = True
        elif command[-2:] == ["set", "--ssh=false"]:
            ssh_enabled = False
        return 0

    client = TailnetClient(
        expected_version="1.98.10",
        login_server="https://headscale.example.test",
        temp_root=tmp_path / "join",
        run_command=run_command,
        inspect_status=inspect_status,
    )

    with pytest.raises(
        TailnetJoinError, match="tailnet_ssh_enable_verification_failed"
    ):
        client.ensure_ssh(
            node_name="edge-abcdef123456", expected_node_id="node-1"
        )

    assert ssh_enabled is False
    assert commands == [
        [
            "/usr/bin/systemctl",
            "is-active",
            "--quiet",
            "drycontroller-tailnet-edge-api.socket",
        ],
        [
            "/usr/bin/tailscale",
            "--socket=/var/run/tailscale/tailscaled.sock",
            "set",
            "--ssh",
        ],
        [
            "/usr/bin/tailscale",
            "--socket=/var/run/tailscale/tailscaled.sock",
            "set",
            "--ssh=false",
        ],
    ]


def test_tailnet_join_rejects_preexisting_unbound_identity_without_consuming_key(tmp_path):
    commands = []
    client = TailnetClient(
        expected_version="1.98.10",
        login_server="https://headscale.example.test",
        temp_root=tmp_path / "join",
        run_command=lambda command: commands.append(command) or 0,
        inspect_status=lambda **_kwargs: {
            "ok": True,
            "reason": "ok",
            "detail": {"node_id": "replacement-node"},
        },
    )

    with pytest.raises(TailnetJoinError, match="tailnet_unexpected_existing_identity"):
        client.join(
            node_name="edge-abcdef123456",
            auth_key="hskey-auth-0123456789abcdef",
        )
    assert commands == [
        ["/usr/bin/systemctl", "is-active", "--quiet", "drycontroller-tailnet-edge-api.socket"]
    ]
    assert not (tmp_path / "join").exists()


def test_tailnet_join_refuses_unobservable_state_without_consuming_key(tmp_path):
    commands = []
    client = TailnetClient(
        expected_version="1.98.10",
        login_server="https://headscale.example.test",
        temp_root=tmp_path / "join",
        run_command=lambda command: commands.append(command) or 0,
        inspect_status=lambda **_kwargs: {
            "ok": False,
            "reason": "tailscale_command_failed",
            "detail": {},
        },
    )

    with pytest.raises(TailnetJoinError, match="tailnet_join_state_unknown"):
        client.join(
            node_name="edge-abcdef123456",
            auth_key="hskey-auth-0123456789abcdef",
        )
    assert commands == []
    assert not (tmp_path / "join").exists()


@pytest.mark.asyncio
async def test_tailnet_status_recovers_durable_join_attempt_after_daemon_success(tmp_path):
    backend, store, _active_path = _components(tmp_path)
    active = _activate_store(backend, store)
    receipt = {
        "schema": ACTIVATION_RECEIPT_SCHEMA,
        "domain": CLOUD_PROOF_DOMAIN,
        "operation": "activate",
        "state": "active",
        "request_id": active["request_id"],
        "device_id": active["device_id"],
        "tenant_id": active["tenant_id"],
        "line_id": active["line_id"],
        "node_name": active["node_name"],
        "identity_fingerprint": active["identity_fingerprint"],
        "runtime_credential_hash": active["runtime_credential_hash"],
        "credential_expires_at": active["credential_expires_at"],
    }
    confirmation = {
        "activation_receipt": receipt,
        "activation_receipt_signature": _signed_proof(receipt),
    }
    tailnet = FakeTailnet()
    tailnet.fail_after_join = True
    app = create_app(
        backend=backend,
        store=store,
        cloud_public_key=_CLOUD_KEY.public_key(),
        tailnet=tailnet,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://factory-edge") as client:
        interrupted = await client.post(
            "/factory-identity/tailnet/join",
            json={**confirmation, "tailnet_auth_key": "hskey-auth-0123456789abcdef"},
        )
        assert interrupted.status_code == 409
        assert store.tailnet_join_attempt() is not None
        assert "hskey-auth" not in store.tailnet_join_attempt_path.read_text(encoding="utf-8")
        recovered = await client.post("/factory-identity/tailnet/status", json=confirmation)

    assert recovered.status_code == 200, recovered.text
    assert store.active()["tailnet_node_id"] == "node-1"
    assert store.tailnet_join_attempt() is None
    assert tailnet.keys == ["hskey-auth-0123456789abcdef"]


@pytest.mark.asyncio
async def test_expired_activation_receipt_cannot_join_or_close_factory_endpoint(tmp_path):
    backend, store, active_path = _components(tmp_path)
    active = _activate_store(backend, store)
    expired = format_timestamp(dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1))
    store._write_json(active_path, {**active, "credential_expires_at": expired})
    receipt = {
        "schema": ACTIVATION_RECEIPT_SCHEMA,
        "domain": CLOUD_PROOF_DOMAIN,
        "operation": "activate",
        "state": "active",
        "request_id": active["request_id"],
        "device_id": active["device_id"],
        "tenant_id": active["tenant_id"],
        "line_id": active["line_id"],
        "node_name": active["node_name"],
        "identity_fingerprint": active["identity_fingerprint"],
        "runtime_credential_hash": active["runtime_credential_hash"],
        "credential_expires_at": expired,
    }
    confirmation = {
        "activation_receipt": receipt,
        "activation_receipt_signature": _signed_proof(receipt),
    }
    app = create_app(
        backend=backend,
        store=store,
        cloud_public_key=_CLOUD_KEY.public_key(),
        tailnet=FakeTailnet(),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://factory-edge") as client:
        status_response = await client.post("/factory-identity/tailnet/status", json=confirmation)
        close_response = await client.post("/factory-identity/close", json=confirmation)

    assert status_response.status_code == 409
    assert status_response.json()["detail"] == "factory_identity_credential_expired"
    assert close_response.status_code == 409
    assert close_response.json()["detail"] == "factory_identity_credential_expired"


@pytest.mark.asyncio
async def test_same_tpm_ssd_recovery_persists_only_after_signed_cloud_receipt(tmp_path):
    backend, store, active_path = _components(tmp_path)
    app = create_app(backend=backend, store=store, cloud_public_key=_CLOUD_KEY.public_key())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://factory-edge") as client:
        identity = (await client.get("/factory-identity")).json()
        device_id = "EDGE-ABCDEF123456"
        candidate_request = {"request_id": identity["request_id"], "device_id": device_id}
        candidate = await client.post("/factory-identity/re-registration", json=candidate_request)
        retry = await client.post("/factory-identity/re-registration", json=candidate_request)
        assert candidate.status_code == 200
        assert retry.json() == candidate.json()
        assert not active_path.exists()

        challenge = {
            "schema": CHALLENGE_SCHEMA,
            "domain": CHALLENGE_DOMAIN,
            "operation": "re_register",
            "device_id": device_id,
            "challenge_id": "00000000-0000-4000-8000-000000000006",
            "challenge_nonce": base64.b64encode(b"s" * 32).decode("ascii"),
            "request_id": identity["request_id"],
            "identity_fingerprint": identity["identity_fingerprint"],
            "runtime_credential_hash": candidate.json()["runtime_credential_hash"],
            "expires_at": format_timestamp(dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=50)),
        }
        wrong = await client.post(
            "/factory-identity/challenge",
            json={**challenge, "identity_fingerprint": "00" * 32},
        )
        signed = await client.post("/factory-identity/challenge", json=challenge)
        assert wrong.status_code == 409
        assert signed.status_code == 200
        public_key = serialization.load_der_public_key(
            base64.b64decode(identity["identity_public_key_spki"], validate=True)
        )
        public_key.verify(
            base64.b64decode(signed.json()["signature"], validate=True),
            canonical_challenge(challenge),
            ec.ECDSA(hashes.SHA256()),
        )

        expiry = format_timestamp(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24))
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
            "runtime_credential_hash": candidate.json()["runtime_credential_hash"],
            "credential_expires_at": expiry,
        }
        confirmation = {
            "activation_receipt": receipt,
            "activation_receipt_signature": _signed_proof(receipt),
        }
        confirmed = await client.post("/factory-identity/confirm", json=confirmation)
        assert confirmed.status_code == 200

    recovered = json.loads(active_path.read_text(encoding="utf-8"))
    assert recovered["device_id"] == device_id
    assert recovered["identity_fingerprint"] == identity["identity_fingerprint"]
    assert recovered["runtime_credential_hash"] == candidate.json()["runtime_credential_hash"]
    assert active_path.stat().st_mode & 0o777 == 0o600
    assert store.candidate() is None


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
