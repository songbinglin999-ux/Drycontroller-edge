from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


HELPER_DIR = Path(__file__).resolve().parents[1]
if str(HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(HELPER_DIR))

from release_engine import (  # noqa: E402
    RELEASE_JOURNAL_SCHEMA,
    SOFTWARE_RELEASE_SERVICES,
    ReleaseEngine,
    ReleaseError,
    ReleasePaths,
    canonical_manifest_digest,
    validate_manifest,
)


REGISTRY = "registry.example.test:5443"
PUBKEY_BYTES = b"test-cosign-public-key"
PUBKEY_DIGEST = "sha256:" + hashlib.sha256(PUBKEY_BYTES).hexdigest()
SIGNATURE = base64.b64encode(b"test-cosign-signature").decode("ascii")


def _manifest(release_id: str, sequence: int, version: str) -> dict:
    return {
        "schema": "drycontroller.software-release.v1",
        "release_id": release_id,
        "sequence": sequence,
        "version": version,
        "source_commit": f"{sequence:x}" * 40,
        "registry": REGISTRY,
        "signing_key_sha256": PUBKEY_DIGEST,
        "services": {
            service: f"{REGISTRY}/drycontroller/{service}@sha256:{sequence * 100 + index:064x}"
            for index, service in enumerate(SOFTWARE_RELEASE_SERVICES, start=1)
        },
    }


def _slot(manifest: dict) -> dict:
    return {
        "manifest": manifest,
        "manifest_digest": canonical_manifest_digest(manifest),
        "manifest_signature": SIGNATURE,
        "verified_at": "2026-08-12T00:00:00+00:00",
    }


def _policy(operation_id: str, manifest: dict) -> dict:
    return {
        "operation_id": operation_id,
        "release_id": manifest["release_id"],
        "manifest_digest": canonical_manifest_digest(manifest),
        "manifest_signature": SIGNATURE,
        "manifest": manifest,
        "tenant_id": "tenant-a",
        "line_id": "line-a",
        "device_id": "EDGE-001",
        "node_name": "edge-001",
        "node_id": int(operation_id[16:], 16),
    }


def _paths(tmp_path: Path) -> ReleasePaths:
    project = tmp_path / "project"
    project.mkdir()
    (project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    pubkey = tmp_path / "cosign.pub"
    pubkey.write_bytes(PUBKEY_BYTES)
    registry_status = tmp_path / "registry-status.json"
    registry_status.write_text(
        json.dumps({"status": "active", "registry_host": REGISTRY}),
        encoding="utf-8",
    )
    device = tmp_path / "device.json"
    device.write_text(
        json.dumps(
            {
                "tenant_id": "tenant-a",
                "line_id": "line-a",
                "device_id": "EDGE-001",
                "node_name": "edge-001",
            }
        ),
        encoding="utf-8",
    )
    request = tmp_path / "request.json"
    cloud = tmp_path / "tailnet.env"
    cloud.write_text(
        "CLOUD_OPS_BASE_URL=https://ops.example.test\nCLOUD_OPS_ENROLL_TOKEN=test-node-token\n",
        encoding="utf-8",
    )
    return ReleasePaths(
        journal=tmp_path / "release-journal.json",
        request=request,
        releases=tmp_path / "releases",
        project=project,
        compose=project / "docker-compose.yml",
        cosign_pubkey=pubkey,
        registry_status=registry_status,
        device=device,
        cloud_config=cloud,
    )


def _seed(paths: ReleasePaths, active: dict, *, previous: dict | None = None) -> None:
    paths.journal.write_text(
        json.dumps(
            {
                "schema": RELEASE_JOURNAL_SCHEMA,
                "phase": "idle",
                "operation_id": None,
                "last_operation_id": None,
                "active": _slot(active),
                "candidate": None,
                "previous": _slot(previous) if previous else None,
                "effect": None,
                "error": None,
            }
        ),
        encoding="utf-8",
    )


def _initial_seed(manifest: dict) -> dict:
    return {
        "manifest": manifest,
        "manifest_digest": canonical_manifest_digest(manifest),
        "manifest_signature": SIGNATURE,
    }


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.current_override: Path | None = None
        self.fail_next_health = False

    @staticmethod
    def _images(override: Path) -> dict[str, str]:
        images: dict[str, str] = {}
        current: str | None = None
        for line in override.read_text(encoding="utf-8").splitlines():
            if line.startswith("  ") and line.endswith(":") and not line.startswith("    "):
                current = line.strip()[:-1]
            elif current and line.strip().startswith("image: "):
                images[current] = json.loads(line.split("image: ", 1)[1])
        return images

    def run(self, args: list[str]) -> str:
        self.calls.append(list(args))
        if args[:3] == ["/usr/bin/docker", "image", "inspect"]:
            return json.dumps([args[-1]])
        if args[:2] == ["/usr/bin/docker", "pull"]:
            return "pulled"
        if args[0] == "/usr/local/bin/cosign":
            return "verified"
        if args[:2] == ["/usr/bin/docker", "compose"]:
            override = Path(args[args.index("--file", args.index("--file") + 1) + 1])
            if "up" in args:
                self.current_override = override
                return "applied"
            if "ps" in args:
                assert self.current_override is not None
                records = []
                for service, image in self._images(self.current_override).items():
                    if service == "mosquitto-init":
                        records.append(
                            {"Service": service, "Image": image, "State": "exited", "ExitCode": 0, "Health": ""}
                        )
                    else:
                        records.append(
                            {"Service": service, "Image": image, "State": "running", "ExitCode": 0, "Health": "healthy"}
                        )
                return json.dumps(records)
            return "configured"
        raise AssertionError(f"unexpected command: {args}")

    def health(self, _url: str) -> None:
        if self.fail_next_health:
            self.fail_next_health = False
            raise ReleaseError("candidate_health_failed")


def test_initial_release_is_verified_and_persisted_only_after_all_images_are_proven(tmp_path):
    paths = _paths(tmp_path)
    runtime = FakeRuntime()
    manifest = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")

    result = ReleaseEngine(paths=paths, command_runner=runtime.run).initialize_active(_initial_seed(manifest))

    journal = json.loads(paths.journal.read_text(encoding="utf-8"))
    assert result["status"] == "initialized"
    assert journal["phase"] == "idle"
    assert journal["active"]["manifest"] == manifest
    assert journal["active"]["manifest_digest"] == canonical_manifest_digest(manifest)
    assert journal["active"]["verified_at"]
    assert journal["candidate"] is None
    assert journal["previous"] is None
    assert any(call[:2] == ["/usr/local/bin/cosign", "verify-blob"] for call in runtime.calls)
    inspected = [call[-1] for call in runtime.calls if call[:3] == ["/usr/bin/docker", "image", "inspect"]]
    assert inspected == [manifest["services"][service] for service in SOFTWARE_RELEASE_SERVICES]

    replay = ReleaseEngine(paths=paths, command_runner=runtime.run).initialize_active(_initial_seed(manifest))
    assert replay["status"] == "already_initialized"


def test_initial_release_failure_never_creates_an_active_journal(tmp_path):
    paths = _paths(tmp_path)
    manifest = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")

    def missing_image(args: list[str]) -> str:
        if args[:3] == ["/usr/bin/docker", "image", "inspect"]:
            return "[]"
        return FakeRuntime().run(args)

    with pytest.raises(ReleaseError, match="local release image missing"):
        ReleaseEngine(paths=paths, command_runner=missing_image).initialize_active(_initial_seed(manifest))

    assert not paths.journal.exists()


def test_initial_release_never_overwrites_a_different_existing_journal(tmp_path):
    paths = _paths(tmp_path)
    release_a = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    release_b = _manifest("22222222-2222-4222-8222-222222222222", 2, "2.0.0")
    _seed(paths, release_a)
    before = paths.journal.read_bytes()

    with pytest.raises(ReleaseError, match="conflicts with existing journal"):
        ReleaseEngine(paths=paths, command_runner=FakeRuntime().run).initialize_active(_initial_seed(release_b))

    assert paths.journal.read_bytes() == before


def _engine(paths: ReleasePaths, runtime: FakeRuntime, policies: dict[str, dict]) -> ReleaseEngine:
    return ReleaseEngine(
        paths=paths,
        command_runner=runtime.run,
        policy_fetcher=lambda operation_id: policies[operation_id],
        health_checker=runtime.health,
    )


def _request(paths: ReleasePaths, operation_id: str) -> None:
    paths.request.write_text(json.dumps({"operation_id": operation_id}), encoding="utf-8")


def test_manifest_rejects_partial_or_mutable_service_sets():
    manifest = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    assert validate_manifest(manifest) == manifest

    partial = {**manifest, "services": dict(manifest["services"])}
    partial["services"].pop("edge-api")
    with pytest.raises(ReleaseError, match="complete service set"):
        validate_manifest(partial)

    tagged = {**manifest, "services": dict(manifest["services"])}
    tagged["services"]["edge-api"] = f"{REGISTRY}/drycontroller/edge-api:latest"
    with pytest.raises(ReleaseError, match="digest locked"):
        validate_manifest(tagged)


def test_manifest_verification_uses_real_cosign_signature(tmp_path):
    cosign = shutil.which("cosign")
    if cosign is None:
        pytest.skip("cosign is not installed")
    paths = _paths(tmp_path)
    key_prefix = tmp_path / "real-cosign"
    signing_config = tmp_path / "signing-config.json"
    environment = {**os.environ, "COSIGN_PASSWORD": "rb4-test-password"}
    subprocess.run(
        [
            cosign,
            "signing-config",
            "create",
            "--no-default-fulcio",
            "--no-default-oidc",
            "--no-default-rekor",
            "--no-default-tsa",
            "--out",
            str(signing_config),
        ],
        check=True,
    )
    subprocess.run(
        [cosign, "generate-key-pair", "--output-key-prefix", str(key_prefix)],
        env=environment,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    paths.cosign_pubkey.write_bytes(key_prefix.with_suffix(".pub").read_bytes())
    manifest = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    manifest["signing_key_sha256"] = "sha256:" + hashlib.sha256(paths.cosign_pubkey.read_bytes()).hexdigest()
    manifest_path = tmp_path / "signed-manifest.json"
    signature_path = tmp_path / "signed-manifest.sigstore.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    subprocess.run(
        [
            cosign,
            "sign-blob",
            "--yes",
            "--signing-config",
            str(signing_config),
            "--key",
            str(key_prefix.with_suffix(".key")),
            "--bundle",
            str(signature_path),
            str(manifest_path),
        ],
        env=environment,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    slot = {
        "manifest": manifest,
        "manifest_digest": canonical_manifest_digest(manifest),
        "manifest_signature": base64.b64encode(signature_path.read_bytes()).decode("ascii"),
        "verified_at": None,
    }

    def real_cosign_runner(args: list[str]) -> str:
        if args[0] == "/usr/local/bin/cosign":
            try:
                completed = subprocess.run(
                    [cosign, *args[1:]],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except subprocess.CalledProcessError as exc:
                raise ReleaseError("real cosign verification failed") from exc
            return completed.stdout
        if args[0] == "/usr/bin/docker" and "config" in args:
            return "configured"
        raise AssertionError(f"unexpected command: {args}")

    engine = ReleaseEngine(paths=paths, command_runner=real_cosign_runner)
    override = engine._materialize_and_verify_manifest(slot)
    assert override.is_file()

    tampered = {**manifest, "version": "1.0.1"}
    tampered_slot = {**slot, "manifest": tampered, "manifest_digest": canonical_manifest_digest(tampered)}
    with pytest.raises(ReleaseError, match="real cosign verification failed"):
        engine._materialize_and_verify_manifest(tampered_slot)


def test_a_to_b_to_c_promotes_only_verified_complete_releases(tmp_path):
    paths = _paths(tmp_path)
    release_a = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    release_b = _manifest("22222222-2222-4222-8222-222222222222", 2, "1.1.0")
    release_c = _manifest("33333333-3333-4333-8333-333333333333", 3, "1.2.0")
    _seed(paths, release_a)
    runtime = FakeRuntime()
    op_b = "00000000000000020000000000000007"
    op_c = "00000000000000030000000000000007"
    engine = _engine(paths, runtime, {op_b: _policy(op_b, release_b), op_c: _policy(op_c, release_c)})

    _request(paths, op_b)
    engine.apply()
    journal = json.loads(paths.journal.read_text(encoding="utf-8"))
    assert journal["active"]["manifest_digest"] == canonical_manifest_digest(release_b)
    assert journal["previous"]["manifest_digest"] == canonical_manifest_digest(release_a)
    assert journal["candidate"] is None

    _request(paths, op_c)
    engine.apply()
    journal = json.loads(paths.journal.read_text(encoding="utf-8"))
    assert journal["active"]["manifest_digest"] == canonical_manifest_digest(release_c)
    assert journal["previous"]["manifest_digest"] == canonical_manifest_digest(release_b)
    assert journal["candidate"] is None
    assert all("--pull" in call and "never" in call for call in runtime.calls if "up" in call)


def test_candidate_failure_rolls_back_previous_without_registry(tmp_path):
    paths = _paths(tmp_path)
    release_a = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    release_b = _manifest("22222222-2222-4222-8222-222222222222", 2, "1.1.0")
    _seed(paths, release_a)
    runtime = FakeRuntime()
    runtime.fail_next_health = True
    operation_id = "00000000000000020000000000000007"
    engine = _engine(paths, runtime, {operation_id: _policy(operation_id, release_b)})
    _request(paths, operation_id)

    with pytest.raises(ReleaseError, match="candidate_health_failed"):
        engine.apply()

    journal = json.loads(paths.journal.read_text(encoding="utf-8"))
    assert journal["phase"] == "idle"
    assert journal["active"]["manifest_digest"] == canonical_manifest_digest(release_a)
    assert journal["candidate"] is None
    up_calls = [call for call in runtime.calls if "up" in call]
    assert len(up_calls) == 2
    assert all("--pull" in call and "never" in call for call in up_calls)
    candidate_up_index = runtime.calls.index(up_calls[0])
    rollback_calls = runtime.calls[candidate_up_index + 1 :]
    assert not any(call[:2] == ["/usr/bin/docker", "pull"] for call in rollback_calls)
    assert not any(call[:2] == ["/usr/local/bin/cosign", "verify"] for call in rollback_calls)


def test_missing_local_previous_image_refuses_before_candidate_mutation(tmp_path):
    paths = _paths(tmp_path)
    release_a = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    release_b = _manifest("22222222-2222-4222-8222-222222222222", 2, "1.1.0")
    _seed(paths, release_a)
    runtime = FakeRuntime()

    def run_with_missing_previous(args: list[str]) -> str:
        if args[:3] == ["/usr/bin/docker", "image", "inspect"] and args[-1] in release_a["services"].values():
            raise ReleaseError("local release image missing")
        return runtime.run(args)

    operation_id = "00000000000000020000000000000007"
    engine = ReleaseEngine(
        paths=paths,
        command_runner=run_with_missing_previous,
        policy_fetcher=lambda _operation_id: _policy(operation_id, release_b),
        health_checker=runtime.health,
    )
    _request(paths, operation_id)

    with pytest.raises(ReleaseError, match="local release image missing"):
        engine.apply()

    journal = json.loads(paths.journal.read_text(encoding="utf-8"))
    assert journal["phase"] == "idle"
    assert journal["active"]["manifest_digest"] == canonical_manifest_digest(release_a)
    assert journal["candidate"] is None
    assert not any("up" in call for call in runtime.calls)


def test_interrupted_candidate_apply_converges_to_previous(tmp_path):
    paths = _paths(tmp_path)
    release_a = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    release_b = _manifest("22222222-2222-4222-8222-222222222222", 2, "1.1.0")
    _seed(paths, release_a)
    journal = json.loads(paths.journal.read_text(encoding="utf-8"))
    journal.update(
        {
            "phase": "candidate_apply_started",
            "operation_id": "00000000000000020000000000000007",
            "candidate": _slot(release_b),
            "previous": _slot(release_a),
            "effect": {"name": "candidate_compose_up", "status": "started"},
        }
    )
    paths.journal.write_text(json.dumps(journal), encoding="utf-8")
    runtime = FakeRuntime()
    operation_id = "00000000000000020000000000000007"
    engine = _engine(paths, runtime, {})
    _request(paths, operation_id)

    with pytest.raises(ReleaseError, match="interrupted_release_recovered"):
        engine.apply()

    recovered = json.loads(paths.journal.read_text(encoding="utf-8"))
    assert recovered["phase"] == "idle"
    assert recovered["active"]["manifest_digest"] == canonical_manifest_digest(release_a)
    assert len([call for call in runtime.calls if "up" in call]) == 1


def test_wrong_device_and_signed_release_replay_fail_before_pull(tmp_path):
    paths = _paths(tmp_path)
    release_a = _manifest("11111111-1111-4111-8111-111111111111", 4, "2.0.0")
    replay = _manifest("22222222-2222-4222-8222-222222222222", 3, "1.9.0")
    _seed(paths, release_a)
    runtime = FakeRuntime()
    operation_id = "00000000000000050000000000000007"
    wrong_device_policy = {**_policy(operation_id, replay), "device_id": "EDGE-OTHER"}
    engine = _engine(paths, runtime, {operation_id: wrong_device_policy})
    _request(paths, operation_id)

    with pytest.raises(ReleaseError, match="policy device binding mismatch"):
        engine.apply()
    assert not any(call[:2] == ["/usr/bin/docker", "pull"] for call in runtime.calls)

    runtime.calls.clear()
    engine = _engine(paths, runtime, {operation_id: _policy(operation_id, replay)})
    with pytest.raises(ReleaseError, match="release sequence is not newer"):
        engine.apply()
    assert not any(call[:2] == ["/usr/bin/docker", "pull"] for call in runtime.calls)


def test_completed_operation_replay_is_idempotent_but_new_operation_is_rejected(tmp_path):
    paths = _paths(tmp_path)
    release_a = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    release_b = _manifest("22222222-2222-4222-8222-222222222222", 2, "1.1.0")
    _seed(paths, release_a)
    runtime = FakeRuntime()
    completed_operation = "00000000000000020000000000000007"
    new_operation = "00000000000000030000000000000007"
    engine = _engine(
        paths,
        runtime,
        {
            completed_operation: _policy(completed_operation, release_b),
            new_operation: _policy(new_operation, release_b),
        },
    )

    _request(paths, completed_operation)
    first = engine.apply()
    calls_after_first = list(runtime.calls)
    assert first["status"] == "succeeded"

    replay = engine.apply()
    assert replay == first
    assert runtime.calls == calls_after_first

    _request(paths, new_operation)
    with pytest.raises(ReleaseError, match="release sequence is not newer"):
        engine.apply()
    assert runtime.calls == calls_after_first
