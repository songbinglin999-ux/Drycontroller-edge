from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


HELPER_DIR = Path(__file__).resolve().parents[1]
if str(HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(HELPER_DIR))

import release_engine  # noqa: E402
from release_engine import (  # noqa: E402
    RELEASE_JOURNAL_SCHEMA,
    COSIGN_TOOLCHAIN,
    ReleaseEngine,
    ReleaseError,
    ReleasePaths,
    canonical_manifest_digest,
    canonical_topology_digest,
    docker_archive_config_digest,
    local_image_reference,
    validate_manifest,
)


REGISTRY = "registry.example.test:5443"
HOST_RELEASE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
HOST_RELEASE_MANIFEST_DIGEST = "sha256:" + "d" * 64
PUBKEY_BYTES = b"test-cosign-public-key"
PUBKEY_DIGEST = "sha256:" + hashlib.sha256(PUBKEY_BYTES).hexdigest()
SIGNATURE = base64.b64encode(b"test-cosign-signature").decode("ascii")
SERVICES = (
    "ai-ctrl-optimizer",
    "ai-softsensor",
    "alertmanager",
    "edge-api",
    "edge-daq",
    "edge-monitor",
    "influxdb",
    "license-service",
    "mosquitto",
    "mosquitto-init",
    "postgres",
    "prometheus",
)
COMPOSE_BYTES = b"services: {}\n"
COMPOSE_DIGEST = "sha256:" + hashlib.sha256(COMPOSE_BYTES).hexdigest()
TOPOLOGY_DIGEST = canonical_topology_digest(COMPOSE_DIGEST, sorted(SERVICES))


def _write_docker_archive(path: Path, manifest: list[dict], configs: dict[str, bytes] | None = None) -> None:
    with tarfile.open(path, mode="w") as archive:
        manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode()
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.size = len(manifest_bytes)
        archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
        for name, config in (configs or {}).items():
            config_info = tarfile.TarInfo(name)
            config_info.size = len(config)
            archive.addfile(config_info, io.BytesIO(config))


def test_docker_archive_config_digest_is_backend_independent_and_fail_closed(tmp_path: Path):
    config = b'{"architecture":"amd64","rootfs":{"type":"layers","diff_ids":[]}}'
    archive = tmp_path / "image.tar"
    _write_docker_archive(
        archive,
        [{"Config": "config.json", "RepoTags": ["example:test"]}],
        {"config.json": config},
    )
    assert docker_archive_config_digest(archive) == f"sha256:{hashlib.sha256(config).hexdigest()}"

    _write_docker_archive(
        archive,
        [
            {"Config": "config.json", "RepoTags": ["example:a"]},
            {"Config": "other.json", "RepoTags": ["example:b"]},
        ],
        {"config.json": config, "other.json": b'{"architecture":"arm64"}'},
    )
    with pytest.raises(ReleaseError, match="exactly one image"):
        docker_archive_config_digest(archive)


def _manifest(release_id: str, sequence: int, version: str) -> dict:
    return {
        "schema": "drycontroller.software-release.v1",
        "release_id": release_id,
        "host_release_id": HOST_RELEASE_ID,
        "topology_digest": TOPOLOGY_DIGEST,
        "sequence": sequence,
        "version": version,
        "source_commit": f"{sequence:x}" * 40,
        "registry": REGISTRY,
        "signing_key_sha256": PUBKEY_DIGEST,
        "cosign": COSIGN_TOOLCHAIN,
        "services": {
            service: f"{REGISTRY}/drycontroller/{service}@sha256:{sequence * 100 + index:064x}"
            for index, service in enumerate(SERVICES, start=1)
        },
        "image_ids": {
            service: f"sha256:{sequence * 100 + index:064x}"
            for index, service in enumerate(SERVICES, start=1)
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
    (project / "docker-compose.yml").write_bytes(COMPOSE_BYTES)
    host_active = tmp_path / "host-active.json"
    host_active.write_text(
        json.dumps(
            {
                "schema": "drycontroller.host-active.v1",
                "host_release_id": HOST_RELEASE_ID,
                "host_release_manifest_digest": HOST_RELEASE_MANIFEST_DIGEST,
                "topology_digest": TOPOLOGY_DIGEST,
                "compose_digest": COMPOSE_DIGEST,
                "service_inventory": sorted(SERVICES),
            }
        ),
        encoding="utf-8",
    )
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
                "schema": "drycontroller.device-identity.v1",
                "tenant_id": "tenant-a",
                "line_id": "line-a",
                "device_id": "EDGE-001",
                "node_name": "edge-001",
                "identity_fingerprint": "11" * 32,
                "runtime_credential": "test-device-credential",
                "runtime_credential_hash": hashlib.sha256(b"test-device-credential").hexdigest(),
                "credential_expires_at": "2099-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    device.chmod(0o600)
    request = tmp_path / "request.json"
    cloud = tmp_path / "cloud-ops.env"
    cloud.write_text(
        "CLOUD_OPS_BASE_URL=https://ops.example.test\n",
        encoding="utf-8",
    )
    return ReleasePaths(
        lock=tmp_path / "release.lock",
        journal=tmp_path / "release-journal.json",
        request=request,
        releases=tmp_path / "releases",
        project=project,
        compose=project / "docker-compose.yml",
        cosign_pubkey=pubkey,
        registry_status=registry_status,
        device=device,
        cloud_config=cloud,
        host_active=host_active,
    )


def _configure_root_docker_auth(tmp_path: Path, monkeypatch, payload: object | None = None) -> Path:
    root = tmp_path / "root-docker-state"
    root.mkdir(mode=0o700)
    source = root / "registry-auth.json"
    source.write_text(json.dumps({"auths": {}} if payload is None else payload), encoding="utf-8")
    source.chmod(0o400)
    monkeypatch.setattr(release_engine, "_REGISTRY_AUTH_SOURCE", source)
    monkeypatch.setattr(release_engine, "_DOCKER_CONFIG_PARENT", root)
    return source


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
    for manifest in (active, previous):
        if manifest is None:
            continue
        digest = canonical_manifest_digest(manifest)
        directory = paths.releases / digest.removeprefix("sha256:")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "local-images.json").write_text(
            json.dumps(
                {
                    "manifest_digest": digest,
                    "images": {
                        service: {
                            "reference": local_image_reference(service, image_id),
                            "image_id": image_id,
                            "runtime_id": image_id,
                        }
                        for service, image_id in manifest["image_ids"].items()
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
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
            if args[-1].startswith("drycontroller.local/"):
                return "sha256:" + args[-1].rsplit(":sha256-", 1)[1]
            return args[-1].rsplit("@", 1)[-1]
        if args[:2] == ["/usr/bin/docker", "tag"]:
            return "tagged"
        if args[:2] == ["/usr/bin/docker", "pull"]:
            return "pulled"
        if args[0] == "/usr/local/bin/cosign":
            return "verified"
        if args and args[0] == release_engine._DOCKER_COMPOSE:
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
    assert paths.lock.stat().st_mode & 0o777 == 0o600
    assert journal["active"]["manifest"] == manifest
    assert journal["active"]["manifest_digest"] == canonical_manifest_digest(manifest)
    assert journal["active"]["verified_at"]
    assert journal["candidate"] is None
    assert journal["previous"] is None
    assert any(call[:2] == ["/usr/local/bin/cosign", "verify-blob"] for call in runtime.calls)
    inspected = [call[-1] for call in runtime.calls if call[:3] == ["/usr/bin/docker", "image", "inspect"]]
    assert inspected == [
        local_image_reference(service, manifest["image_ids"][service]) for service in SERVICES
    ]

    replay = ReleaseEngine(paths=paths, command_runner=runtime.run).initialize_active(_initial_seed(manifest))
    assert replay["status"] == "already_initialized"


def test_initial_release_failure_never_creates_an_active_journal(tmp_path):
    paths = _paths(tmp_path)
    manifest = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")

    def missing_image(args: list[str]) -> str:
        if args[:3] == ["/usr/bin/docker", "image", "inspect"]:
            return "sha256:" + "0" * 64
        return FakeRuntime().run(args)

    with pytest.raises(ReleaseError, match="release image ID mismatch"):
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


def test_initializer_can_reuse_the_factory_transaction_lock_descriptor(tmp_path):
    paths = _paths(tmp_path)
    manifest = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    paths.lock.touch(mode=0o600)
    descriptor = os.open(paths.lock, os.O_WRONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = ReleaseEngine(paths=paths, command_runner=FakeRuntime().run).initialize_active_with_lock_fd(
            _initial_seed(manifest), descriptor
        )
    finally:
        os.close(descriptor)

    assert result["status"] == "initialized"


def test_starter_can_reuse_the_host_transaction_lock_descriptor(tmp_path):
    paths = _paths(tmp_path)
    manifest = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    _seed(paths, manifest)
    paths.lock.touch(mode=0o600)
    descriptor = os.open(paths.lock, os.O_WRONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = ReleaseEngine(
            paths=paths,
            command_runner=FakeRuntime().run,
            health_checker=lambda _url: None,
        ).start_active_with_lock_fd(descriptor)
    finally:
        os.close(descriptor)

    assert result["status"] == "running"


def test_starter_rejects_an_unlocked_release_lock_descriptor(tmp_path):
    paths = _paths(tmp_path)
    manifest = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    _seed(paths, manifest)
    paths.lock.touch(mode=0o600)
    descriptor = os.open(paths.lock, os.O_WRONLY)
    try:
        with pytest.raises(ReleaseError, match="is not held"):
            ReleaseEngine(
                paths=paths,
                command_runner=FakeRuntime().run,
                health_checker=lambda _url: None,
            ).start_active_with_lock_fd(descriptor)
    finally:
        os.close(descriptor)


def _engine(paths: ReleasePaths, runtime: FakeRuntime, policies: dict[str, dict]) -> ReleaseEngine:
    return ReleaseEngine(
        paths=paths,
        command_runner=runtime.run,
        policy_fetcher=lambda operation_id: policies[operation_id],
        health_checker=runtime.health,
    )


def _request(paths: ReleasePaths, operation_id: str) -> None:
    paths.request.write_text(json.dumps({"operation_id": operation_id}), encoding="utf-8")


def test_manifest_accepts_topology_scoped_sets_and_rejects_mismatched_or_mutable_images():
    manifest = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    assert validate_manifest(manifest) == manifest

    partial = {**manifest, "services": dict(manifest["services"])}
    partial["services"].pop("edge-api")
    with pytest.raises(ReleaseError, match="image ID set must match"):
        validate_manifest(partial)

    scoped = {
        **partial,
        "image_ids": {service: manifest["image_ids"][service] for service in partial["services"]},
    }
    assert validate_manifest(scoped) == scoped

    tagged = {**manifest, "services": dict(manifest["services"])}
    tagged["services"]["edge-api"] = f"{REGISTRY}/drycontroller/edge-api:latest"
    with pytest.raises(ReleaseError, match="digest locked"):
        validate_manifest(tagged)

    missing_image_id = {**manifest, "image_ids": dict(manifest["image_ids"])}
    missing_image_id["image_ids"].pop("edge-api")
    with pytest.raises(ReleaseError, match="image ID set must match"):
        validate_manifest(missing_image_id)

    unapproved_cosign = {
        **manifest,
        "cosign": {"version": "v3.1.2", "linux_amd64_sha256": "sha256:" + "0" * 64},
    }
    with pytest.raises(ReleaseError, match="not approved"):
        validate_manifest(unapproved_cosign)


def test_initial_release_rejects_wrong_host_binding_before_runtime_side_effects(tmp_path):
    paths = _paths(tmp_path)
    runtime = FakeRuntime()
    manifest = {
        **_manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0"),
        "host_release_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    }

    with pytest.raises(ReleaseError, match="HostRelease binding mismatch"):
        ReleaseEngine(paths=paths, command_runner=runtime.run).initialize_active(_initial_seed(manifest))

    assert runtime.calls == []
    assert not paths.journal.exists()


@pytest.mark.parametrize("mismatch", ["topology", "inventory"])
def test_candidate_host_binding_mismatch_is_rejected_before_pull(tmp_path, mismatch):
    paths = _paths(tmp_path)
    runtime = FakeRuntime()
    operation_id = "00000000000000010000000000000002"
    active = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    candidate = _manifest("22222222-2222-4222-8222-222222222222", 2, "1.1.0")
    if mismatch == "topology":
        candidate = {**candidate, "topology_digest": "sha256:" + "e" * 64}
    else:
        candidate = {
            **candidate,
            "services": {key: value for key, value in candidate["services"].items() if key != "edge-api"},
            "image_ids": {key: value for key, value in candidate["image_ids"].items() if key != "edge-api"},
        }
    _seed(paths, active)
    before = paths.journal.read_bytes()
    _request(paths, operation_id)

    with pytest.raises(ReleaseError, match="binding mismatch|service inventory"):
        _engine(paths, runtime, {operation_id: _policy(operation_id, candidate)}).apply()

    assert not any(call[:2] == ["/usr/bin/docker", "pull"] for call in runtime.calls)
    assert paths.journal.read_bytes() == before


def test_active_compose_digest_drift_is_rejected_before_start(tmp_path):
    paths = _paths(tmp_path)
    runtime = FakeRuntime()
    active = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    _seed(paths, active)
    before = paths.journal.read_bytes()
    paths.compose.write_text("services:\n  drift: {}\n", encoding="utf-8")

    with pytest.raises(ReleaseError, match="Compose digest mismatch"):
        _engine(paths, runtime, {}).start_active()

    assert runtime.calls == []
    assert paths.journal.read_bytes() == before


def test_signed_active_start_uses_local_image_ids_and_never_pulls(tmp_path):
    paths = _paths(tmp_path)
    runtime = FakeRuntime()
    active = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    _seed(paths, active)

    result = _engine(paths, runtime, {}).start_active()

    assert result["status"] == "running"
    compose_up = next(
        call for call in runtime.calls if call and call[0] == release_engine._DOCKER_COMPOSE and "up" in call
    )
    assert compose_up[-3:] == ["--pull", "never", "--no-build"]
    assert not any(call[:2] == ["/usr/bin/docker", "pull"] for call in runtime.calls)
    assert runtime.current_override is not None
    assert runtime._images(runtime.current_override) == {
        service: local_image_reference(service, image_id)
        for service, image_id in active["image_ids"].items()
    }
    journal = json.loads(paths.journal.read_text(encoding="utf-8"))
    assert journal["phase"] == "idle"
    assert journal["effect"] is None
    assert paths.lock.is_file()


def test_signed_active_waits_for_compose_start_period_and_transient_http_readiness(tmp_path):
    paths = _paths(tmp_path)
    runtime = FakeRuntime()
    active = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    _seed(paths, active)
    status_checks = 0
    health_checks = 0

    def command(args: list[str]) -> str:
        nonlocal status_checks
        output = runtime.run(args)
        if args and args[0] == release_engine._DOCKER_COMPOSE and "ps" in args:
            status_checks += 1
            if status_checks == 1:
                records = json.loads(output)
                for record in records:
                    if record["Service"] != "mosquitto-init":
                        record["Health"] = "starting"
                return json.dumps(records)
        return output

    def health(_url: str) -> None:
        nonlocal health_checks
        health_checks += 1
        if health_checks == 1:
            raise ReleaseError("release health check failed")

    result = ReleaseEngine(
        paths=paths,
        command_runner=command,
        health_checker=health,
        sleeper=lambda _seconds: None,
    ).start_active()

    assert result["status"] == "running"
    assert status_checks == 3
    assert health_checks == 3


def test_signed_active_pending_service_does_not_hide_later_image_mismatch(tmp_path):
    paths = _paths(tmp_path)
    runtime = FakeRuntime()
    active = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    _seed(paths, active)
    sleeps = 0

    def status(args: list[str], _timeout: float) -> str:
        records = json.loads(runtime.run(args))
        records[0]["Health"] = "starting"
        records[-1]["Image"] = "sha256:" + "f" * 64
        return json.dumps(records)

    def sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1

    with pytest.raises(ReleaseError, match="running image does not match release manifest"):
        ReleaseEngine(
            paths=paths,
            command_runner=runtime.run,
            runtime_command_runner=status,
            health_checker=runtime.health,
            sleeper=sleep,
        ).start_active()

    assert sleeps == 0


def test_runtime_compose_probe_receives_remaining_deadline_and_timeout_is_fail_closed(tmp_path):
    paths = _paths(tmp_path)
    runtime = FakeRuntime()
    active = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    _seed(paths, active)
    timeouts: list[float] = []

    def timed_out(_args: list[str], timeout: float) -> str:
        timeouts.append(timeout)
        raise ReleaseError("command timed out: docker")

    with pytest.raises(ReleaseError, match="command timed out: docker"):
        ReleaseEngine(
            paths=paths,
            command_runner=runtime.run,
            runtime_command_runner=timed_out,
            health_checker=runtime.health,
            runtime_ready_timeout=7,
        ).start_active()

    assert len(timeouts) == 1
    assert 0 < timeouts[0] <= 7


def test_subprocess_timeout_is_mapped_to_release_error(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _configure_root_docker_auth(tmp_path, monkeypatch)
    monkeypatch.setattr(release_engine, "_validate_trusted_executable", lambda *_args: None)

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ReleaseError, match="command timed out: docker"):
        ReleaseEngine(paths=paths)._run_command(["/usr/bin/docker", "version"], timeout=1)


def test_software_docker_commands_require_one_fixed_trusted_executable(tmp_path, monkeypatch):
    _configure_root_docker_auth(tmp_path, monkeypatch)
    trusted_dir = tmp_path / "trusted" / "bin"
    trusted_dir.mkdir(parents=True)
    trusted = trusted_dir / "docker"
    trusted.write_text("#!/bin/sh\necho trusted\n", encoding="utf-8")
    trusted.chmod(0o755)
    monkeypatch.setattr(release_engine, "_DOCKER", str(trusted))
    engine = ReleaseEngine(paths=_paths(tmp_path))

    assert engine._run_command([str(trusted), "version"]).strip() == "trusted"

    trusted.chmod(0o777)
    with pytest.raises(ReleaseError, match="untrusted"):
        engine._run_command([str(trusted), "version"])
    trusted.chmod(0o755)

    symlink = trusted_dir / "docker-link"
    symlink.symlink_to(trusted)
    monkeypatch.setattr(release_engine, "_DOCKER", str(symlink))
    with pytest.raises(ReleaseError, match="untrusted"):
        engine._run_command([str(symlink), "version"])

    drift = tmp_path / "path-drift" / "docker"
    drift.parent.mkdir()
    drift.write_text("#!/bin/sh\necho drift\n", encoding="utf-8")
    drift.chmod(0o755)
    monkeypatch.setattr(release_engine, "_DOCKER", str(trusted))
    with pytest.raises(ReleaseError, match="path drift"):
        engine._run_command([str(drift), "version"])


@pytest.mark.parametrize(
    "extra_key",
    ["cli-plugins", "context", "currentContext", "plugins", "proxies", "credsStore", "credHelpers", "unknown"],
)
def test_software_root_docker_auth_source_rejects_every_non_auths_top_level_key(
    tmp_path,
    monkeypatch,
    extra_key,
):
    _configure_root_docker_auth(tmp_path, monkeypatch, {"auths": {}, extra_key: {}})
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    docker.chmod(0o755)
    monkeypatch.setattr(release_engine, "_DOCKER", str(docker))

    with pytest.raises(ReleaseError, match="schema"):
        ReleaseEngine(paths=_paths(tmp_path))._run_command([str(docker), "version"])


def test_software_compose_uses_fixed_binary_and_private_sanitized_config(tmp_path, monkeypatch):
    auth = base64.b64encode(b"reader:secret").decode("ascii")
    _configure_root_docker_auth(tmp_path, monkeypatch, {"auths": {REGISTRY: {"auth": auth}}})
    compose = tmp_path / "docker-compose"
    compose.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$DOCKER_HOST\"\n"
        "printf '%s\\n' \"$DOCKER_CONFIG\"\n"
        "cat \"$DOCKER_CONFIG/config.json\"\n",
        encoding="utf-8",
    )
    compose.chmod(0o755)
    monkeypatch.setattr(release_engine, "_DOCKER_COMPOSE", str(compose))

    output = ReleaseEngine(paths=_paths(tmp_path))._run_command([str(compose), "version"])
    lines = output.splitlines()

    assert lines[0] == "unix:///var/run/docker.sock"
    assert json.loads(lines[2]) == {"auths": {REGISTRY: {"auth": auth}}}
    assert not Path(lines[1]).exists()


def _install_nonblocking_lock_probe(blocked, acquired_without_waiting) -> None:
    import release_engine

    original_flock = release_engine.fcntl.flock
    probed = False

    def probe(descriptor, operation):
        nonlocal probed
        if not probed and operation & release_engine.fcntl.LOCK_EX:
            probed = True
            try:
                original_flock(descriptor, operation | release_engine.fcntl.LOCK_NB)
            except BlockingIOError:
                blocked.set()
                return original_flock(descriptor, operation)
            acquired_without_waiting.set()
            return None
        return original_flock(descriptor, operation)

    release_engine.fcntl.flock = probe


def test_apply_and_active_start_use_a_two_process_transaction_barrier(tmp_path):
    paths = _paths(tmp_path)
    release_a = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    release_b = _manifest("22222222-2222-4222-8222-222222222222", 2, "1.1.0")
    _seed(paths, release_a)
    operation_id = "00000000000000020000000000000007"
    _request(paths, operation_id)
    policies = {operation_id: _policy(operation_id, release_b)}
    context = multiprocessing.get_context("fork")
    apply_entered = context.Event()
    release_apply = context.Event()
    start_blocked = context.Event()
    start_acquired_early = context.Event()
    start_side_effect = context.Event()
    results = context.Queue()

    def run_apply() -> None:
        runtime = FakeRuntime()
        blocked_once = False

        def command(args: list[str]) -> str:
            nonlocal blocked_once
            result = runtime.run(args)
            if not blocked_once and args and args[0] == release_engine._DOCKER_COMPOSE and "up" in args:
                blocked_once = True
                apply_entered.set()
                if not release_apply.wait(10):
                    raise RuntimeError("apply barrier was not released")
            return result

        result = ReleaseEngine(
            paths=paths,
            command_runner=command,
            policy_fetcher=lambda requested: policies[requested],
            health_checker=runtime.health,
        ).apply()
        results.put(("apply", result["status"]))

    def run_start() -> None:
        _install_nonblocking_lock_probe(start_blocked, start_acquired_early)
        runtime = FakeRuntime()

        def command(args: list[str]) -> str:
            start_side_effect.set()
            return runtime.run(args)

        result = ReleaseEngine(paths=paths, command_runner=command, health_checker=runtime.health).start_active()
        results.put(("start", result["status"]))

    apply_process = context.Process(target=run_apply)
    start_process = context.Process(target=run_start)
    try:
        apply_process.start()
        assert apply_entered.wait(5)
        start_process.start()
        assert start_blocked.wait(5)
        assert not start_acquired_early.is_set()
        assert not start_side_effect.is_set()
        release_apply.set()
        apply_process.join(10)
        start_process.join(10)
        assert apply_process.exitcode == 0
        assert start_process.exitcode == 0
        assert {results.get(timeout=1), results.get(timeout=1)} == {("apply", "succeeded"), ("start", "running")}
    finally:
        release_apply.set()
        for process in (apply_process, start_process):
            if process.is_alive():
                process.terminate()
            process.join(5)


def test_initializer_and_apply_use_a_two_process_transaction_barrier(tmp_path):
    paths = _paths(tmp_path)
    release_a = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    release_b = _manifest("22222222-2222-4222-8222-222222222222", 2, "1.1.0")
    operation_id = "00000000000000020000000000000007"
    _request(paths, operation_id)
    policies = {operation_id: _policy(operation_id, release_b)}
    context = multiprocessing.get_context("fork")
    initializer_entered = context.Event()
    release_initializer = context.Event()
    apply_blocked = context.Event()
    apply_acquired_early = context.Event()
    apply_side_effect = context.Event()
    results = context.Queue()

    def run_initializer() -> None:
        runtime = FakeRuntime()
        blocked_once = False

        def command(args: list[str]) -> str:
            nonlocal blocked_once
            if not blocked_once and args[0] == "/usr/local/bin/cosign":
                blocked_once = True
                initializer_entered.set()
                if not release_initializer.wait(10):
                    raise RuntimeError("initializer barrier was not released")
            return runtime.run(args)

        result = ReleaseEngine(paths=paths, command_runner=command).initialize_active(_initial_seed(release_a))
        results.put(("initialize", result["status"]))

    def run_apply() -> None:
        _install_nonblocking_lock_probe(apply_blocked, apply_acquired_early)
        runtime = FakeRuntime()

        def command(args: list[str]) -> str:
            apply_side_effect.set()
            return runtime.run(args)

        result = ReleaseEngine(
            paths=paths,
            command_runner=command,
            policy_fetcher=lambda requested: policies[requested],
            health_checker=runtime.health,
        ).apply()
        results.put(("apply", result["status"]))

    initializer_process = context.Process(target=run_initializer)
    apply_process = context.Process(target=run_apply)
    try:
        initializer_process.start()
        assert initializer_entered.wait(5)
        apply_process.start()
        assert apply_blocked.wait(5)
        assert not apply_acquired_early.is_set()
        assert not apply_side_effect.is_set()
        release_initializer.set()
        initializer_process.join(10)
        apply_process.join(10)
        assert initializer_process.exitcode == 0
        assert apply_process.exitcode == 0
        assert {results.get(timeout=1), results.get(timeout=1)} == {
            ("initialize", "initialized"),
            ("apply", "succeeded"),
        }
    finally:
        release_initializer.set()
        for process in (initializer_process, apply_process):
            if process.is_alive():
                process.terminate()
            process.join(5)


def test_killing_the_lock_holder_releases_the_kernel_lock(tmp_path):
    paths = _paths(tmp_path)
    active = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    _seed(paths, active)
    context = multiprocessing.get_context("fork")
    holder_ready = context.Event()
    start_blocked = context.Event()
    start_acquired_early = context.Event()
    results = context.Queue()

    def hold_lock() -> None:
        with ReleaseEngine(paths=paths, command_runner=FakeRuntime().run)._transaction_lock():
            holder_ready.set()
            context.Event().wait()

    def run_start() -> None:
        _install_nonblocking_lock_probe(start_blocked, start_acquired_early)
        runtime = FakeRuntime()
        result = ReleaseEngine(paths=paths, command_runner=runtime.run, health_checker=runtime.health).start_active()
        results.put(result["status"])

    holder = context.Process(target=hold_lock)
    start_process = context.Process(target=run_start)
    try:
        holder.start()
        assert holder_ready.wait(5)
        start_process.start()
        assert start_blocked.wait(5)
        assert not start_acquired_early.is_set()
        holder.terminate()
        holder.join(5)
        start_process.join(10)
        assert holder.exitcode is not None and holder.exitcode != 0
        assert start_process.exitcode == 0
        assert results.get(timeout=1) == "running"
    finally:
        for process in (holder, start_process):
            if process.is_alive():
                process.terminate()
            process.join(5)


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
        if args[0] == release_engine._DOCKER_COMPOSE and "config" in args:
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
    assert all(
        "--pull" in call and "never" in call and "--no-build" in call
        for call in runtime.calls
        if "up" in call
    )
    assert paths.lock.is_file()


def test_candidate_failure_rolls_back_previous_without_registry(tmp_path):
    paths = _paths(tmp_path)
    release_a = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    release_b = _manifest("22222222-2222-4222-8222-222222222222", 2, "1.1.0")
    _seed(paths, release_a)
    runtime = FakeRuntime()
    operation_id = "00000000000000020000000000000007"
    clock = 0.0

    def health(_url: str) -> None:
        assert runtime.current_override is not None
        if runtime._images(runtime.current_override) == {
            service: local_image_reference(service, image_id)
            for service, image_id in release_b["image_ids"].items()
        }:
            raise ReleaseError("candidate_health_failed")

    def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    engine = ReleaseEngine(
        paths=paths,
        command_runner=runtime.run,
        policy_fetcher=lambda requested: {operation_id: _policy(operation_id, release_b)}[requested],
        health_checker=health,
        monotonic=lambda: clock,
        sleeper=sleep,
        runtime_ready_timeout=1,
        runtime_ready_poll=1,
    )
    _request(paths, operation_id)

    with pytest.raises(ReleaseError, match="readiness deadline exceeded"):
        engine.apply()

    journal = json.loads(paths.journal.read_text(encoding="utf-8"))
    assert journal["phase"] == "idle"
    assert journal["active"]["manifest_digest"] == canonical_manifest_digest(release_a)
    assert journal["candidate"] is None
    up_calls = [call for call in runtime.calls if "up" in call]
    assert len(up_calls) == 2
    assert all("--pull" in call and "never" in call and "--no-build" in call for call in up_calls)
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
        if args[:3] == ["/usr/bin/docker", "image", "inspect"] and args[-1] in {
            local_image_reference(service, image_id)
            for service, image_id in release_a["image_ids"].items()
        }:
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


def test_candidate_registry_reference_must_resolve_to_the_signed_image_id(tmp_path):
    paths = _paths(tmp_path)
    release_a = _manifest("11111111-1111-4111-8111-111111111111", 1, "1.0.0")
    release_b = _manifest("22222222-2222-4222-8222-222222222222", 2, "1.1.0")
    _seed(paths, release_a)
    runtime = FakeRuntime()

    def run_with_wrong_candidate_id(args: list[str]) -> str:
        if args[:3] == ["/usr/bin/docker", "image", "inspect"] and args[-1] in {
            local_image_reference(service, image_id)
            for service, image_id in release_b["image_ids"].items()
        }:
            return "sha256:" + "0" * 64
        return runtime.run(args)

    operation_id = "00000000000000020000000000000007"
    engine = ReleaseEngine(
        paths=paths,
        command_runner=run_with_wrong_candidate_id,
        policy_fetcher=lambda _operation_id: _policy(operation_id, release_b),
        health_checker=runtime.health,
    )
    _request(paths, operation_id)

    with pytest.raises(ReleaseError, match="release image ID mismatch"):
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
