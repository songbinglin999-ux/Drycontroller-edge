from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import sys

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils


HELPER = Path(__file__).resolve().parents[1]
ROOT = HELPER.parents[1]
for entry in (str(HELPER), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import edge_app_release as release  # noqa: E402


class Crash(BaseException):
    pass


def _signature(key: ec.EllipticCurvePrivateKey, manifest: dict) -> str:
    digest = hashlib.sha256(release.canonical_json_bytes(manifest)).digest()
    signature = key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    public_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    bundle = {
        "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
        "verificationMaterial": {
            "publicKey": {"hint": base64.b64encode(hashlib.sha256(public_der).digest()).decode()}
        },
        "messageSignature": {
            "messageDigest": {
                "algorithm": "SHA2_256",
                "digest": base64.b64encode(digest).decode(),
            },
            "signature": base64.b64encode(signature).decode(),
        },
    }
    return base64.b64encode(json.dumps(bundle, separators=(",", ":")).encode()).decode()


def _manifest(paths: release.ReleasePaths, *, release_id: str, sequence: int, suffix: str) -> dict:
    return {
        "schema": "drycontroller.app-release.v2",
        "profile": "edge-app-v1",
        "release_id": release_id,
        "sequence": sequence,
        "source_commit": suffix * 40,
        "deployment_digest": release.deployment_digest(paths.compose),
        "images": {
            "edge-api": f"registry.test/edge-api@sha256:{suffix * 64}",
            "edge-daq": f"registry.test/edge-daq@sha256:{suffix * 64}",
        },
    }


class Commands:
    def __init__(self, running: dict[str, str]):
        self.running = dict(running)
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        if args[:2] == ["/usr/bin/docker", "inspect"]:
            service = args[2]
            return json.dumps(
                [
                    {
                        "Config": {"Image": self.running[service]},
                        "State": {"Health": {"Status": "healthy"}},
                    }
                ]
            )
        if args[0].endswith("docker-compose"):
            override = Path(args[args.index("--file", 3) + 1]).read_text()
            for service in release.EDGE_SERVICES:
                marker = '    image: "'
                section = override.split(f"  {service}:\n", 1)[1]
                self.running[service] = section.split(marker, 1)[1].split('"', 1)[0]
        return "[]"


def _fixture(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    compose = tmp_path / "project" / "docker-compose.yml"
    compose.parent.mkdir()
    compose.write_text("services:\n  edge-api: {}\n  edge-daq: {}\n")
    paths = release.ReleasePaths(
        lock=state / "release.lock",
        journal=state / "release-journal.json",
        request=state / "request.json",
        active=state / "active.json",
        previous=state / "previous.json",
        candidate=state / "candidate.json",
        history=state / "history",
        project=compose.parent,
        compose=compose,
        cosign_pubkey=tmp_path / "cosign.pub",
        device=tmp_path / "device.json",
        tpm_observation=tmp_path / "tpm.json",
        license=tmp_path / "license.json",
        license_trust_policy=tmp_path / "policy.json",
        cloud_config=tmp_path / "cloud.env",
    )
    key = ec.generate_private_key(ec.SECP256R1())
    paths.cosign_pubkey.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    identity = {
        "tenant_id": "tenant-a",
        "line_id": "LINE-A",
        "device_id": "EDGE-ABCDEF123456",
        "node_name": "edge-abcdef123456",
    }
    paths.device.write_text(json.dumps(identity))
    active = _manifest(
        paths,
        release_id="11111111-1111-4111-8111-111111111111",
        sequence=1,
        suffix="1",
    )
    candidate = _manifest(
        paths,
        release_id="22222222-2222-4222-8222-222222222222",
        sequence=2,
        suffix="2",
    )
    commands = Commands(active["images"])
    monkeypatch.setattr(release, "verify_installed_license", lambda **_kwargs: {"valid": True})
    engine = release.ReleaseEngine(
        paths=paths,
        command_runner=commands,
        health_checker=lambda _url: None,
    )
    engine.initialize_active(
        {
            "manifest": active,
            "manifest_digest": release.canonical_manifest_digest(active),
            "manifest_signature": _signature(key, active),
        }
    )
    operation_id = "0" * 31 + "1"
    release.atomic_write(paths.request, {"operation_id": operation_id})
    policy = {
        "operation_id": operation_id,
        "release_id": candidate["release_id"],
        "manifest_digest": release.canonical_manifest_digest(candidate),
        "manifest_signature": _signature(key, candidate),
        "manifest": candidate,
        "tenant_id": identity["tenant_id"],
        "line_id": identity["line_id"],
        "device_id": identity["device_id"],
        "node_name": identity["node_name"],
        "node_id": 1,
    }
    return paths, key, active, candidate, commands, policy


def _engine(paths, commands, policy, **kwargs):
    return release.ReleaseEngine(
        paths=paths,
        command_runner=commands,
        policy_fetcher=lambda _operation_id: policy,
        health_checker=lambda _url: None,
        **kwargs,
    )


def test_apply_and_offline_rollback_mutate_only_two_services(tmp_path: Path, monkeypatch):
    paths, _key, active, candidate, commands, policy = _fixture(tmp_path, monkeypatch)
    commands.calls.clear()

    result = _engine(paths, commands, policy).apply()

    assert result["release_id"] == candidate["release_id"]
    compose_calls = [args for args in commands.calls if args[0].endswith("docker-compose")]
    assert len(compose_calls) == 1
    assert compose_calls[0][-2:] == ["edge-api", "edge-daq"]
    assert "--no-deps" in compose_calls[0]
    assert compose_calls[0][compose_calls[0].index("--pull") + 1] == "never"
    assert "--remove-orphans" not in compose_calls[0]
    assert release.validate_slot(release.read_json(paths.previous))["manifest"] == active

    commands.calls.clear()
    rollback = _engine(paths, commands, policy).rollback()
    assert rollback["release_id"] == active["release_id"]
    assert not any(args[:2] == ["/usr/bin/docker", "pull"] for args in commands.calls)


@pytest.mark.parametrize("failure", ["license", "signature", "device"])
def test_identity_license_and_signature_fail_before_pull_or_compose(
    tmp_path: Path, monkeypatch, failure: str
):
    paths, _key, _active, _candidate, commands, policy = _fixture(tmp_path, monkeypatch)
    if failure == "license":
        monkeypatch.setattr(
            release,
            "verify_installed_license",
            lambda **_kwargs: {"valid": False, "reason": "missing"},
        )
    elif failure == "signature":
        policy["manifest_signature"] = base64.b64encode(b"{}").decode()
    else:
        policy["device_id"] = "EDGE-000000000000"
    commands.calls.clear()

    with pytest.raises(release.ReleaseError):
        _engine(paths, commands, policy).apply()

    assert not any(args[:2] == ["/usr/bin/docker", "pull"] for args in commands.calls)
    assert not any(args[0].endswith("docker-compose") for args in commands.calls)


def test_boot_recovery_restores_old_active_after_mutation_crash(tmp_path: Path, monkeypatch):
    paths, _key, active, _candidate, commands, policy = _fixture(tmp_path, monkeypatch)

    def crash(point: str) -> None:
        if point == "after_mutated":
            raise Crash(point)

    with pytest.raises(Crash):
        _engine(paths, commands, policy, fault=crash).apply()
    assert release.read_json(paths.journal)["phase"] == "mutated"

    commands.calls.clear()
    recovered = _engine(paths, commands, policy).recover()
    assert recovered["release_id"] == active["release_id"]
    assert commands.running == active["images"]
    assert release.read_json(paths.journal)["phase"] == "idle"


def test_manifest_rejects_legacy_or_third_service():
    with pytest.raises(release.ReleaseError, match="canonical v2"):
        release.validate_manifest({"schema": "drycontroller.software-release.v1"})

    manifest = {
        "schema": "drycontroller.app-release.v2",
        "profile": "edge-app-v1",
        "release_id": "11111111-1111-4111-8111-111111111111",
        "sequence": 1,
        "source_commit": "1" * 40,
        "deployment_digest": "sha256:" + "1" * 64,
        "images": {
            "edge-api": "registry/edge-api@sha256:" + "1" * 64,
            "edge-daq": "registry/edge-daq@sha256:" + "1" * 64,
            "license-service": "registry/license@sha256:" + "1" * 64,
        },
    }
    with pytest.raises(release.ReleaseError, match="exactly"):
        release.validate_manifest(manifest)
