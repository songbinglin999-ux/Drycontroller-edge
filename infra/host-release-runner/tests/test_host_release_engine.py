from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tarfile
import time
from pathlib import Path

import pytest


RUNNER_DIR = Path(__file__).resolve().parents[1]
if str(RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(RUNNER_DIR))

import host_release_engine  # noqa: E402
from host_release_engine import (  # noqa: E402
    COORDINATOR_JOURNAL_SCHEMA,
    COSIGN_TOOLCHAIN,
    HOST_HEALTH_CONTRACT,
    HostReleaseEngine,
    HostReleaseError,
    HostReleasePaths,
    _local_image_reference,
    canonical_digest,
    canonical_topology_digest,
    validate_host_manifest,
)


REGISTRY = "registry.example.test:5443"
HOST_KEY = b"host-release-public-key"
SOFTWARE_KEY = b"software-release-public-key"
SIGNATURE = base64.b64encode(b"{}").decode("ascii")
HELPERS = {
    "infra/ota-helper/drycontroller_ota_helper.py": (b"helper\n", 0o644),
    "infra/ota-helper/release_engine.py": (b"engine\n", 0o644),
    "infra/ota-helper/ota-apply": (b"#!/usr/bin/python3\n", 0o755),
    "infra/ota-helper/initialize-active-release": (b"#!/usr/bin/python3\n", 0o755),
    "infra/ota-helper/start-active-release": (b"#!/usr/bin/python3\n", 0o755),
}


def _files(sequence: int) -> dict[str, tuple[bytes, int]]:
    return {
        "docker-compose.yml": (
            f"services:\n  edge-api:\n    image: edge-api:{sequence}\n".encode(),
            0o644,
        ),
        **HELPERS,
        "configs/tags.yaml": (f"version: {sequence}\n".encode(), 0o644),
    }


def _software(release_id: str, host_id: str, topology_digest: str, sequence: int) -> tuple[dict, bytes]:
    config = json.dumps({"service": "edge-api", "sequence": sequence}, separators=(",", ":")).encode()
    image_id = f"sha256:{hashlib.sha256(config).hexdigest()}"
    return (
        {
            "schema": "drycontroller.software-release.v1",
            "release_id": release_id,
            "host_release_id": host_id,
            "topology_digest": topology_digest,
            "sequence": sequence,
            "version": f"{sequence}.0.0",
            "source_commit": f"{sequence:x}" * 40,
            "registry": REGISTRY,
            "signing_key_sha256": f"sha256:{hashlib.sha256(SOFTWARE_KEY).hexdigest()}",
            "cosign": COSIGN_TOOLCHAIN,
            "services": {
                "edge-api": f"{REGISTRY}/drycontroller/edge-api@sha256:{sequence:064x}",
            },
            "image_ids": {"edge-api": image_id},
        },
        config,
    )


def _host(
    release_id: str,
    software: dict,
    sequence: int,
    files: dict[str, tuple[bytes, int]],
    bundle_config: bytes,
) -> dict:
    compose = files["docker-compose.yml"][0]
    topology = {
        "compose_digest": f"sha256:{hashlib.sha256(compose).hexdigest()}",
        "service_inventory": ["edge-api"],
        "health_contract": HOST_HEALTH_CONTRACT,
    }
    return {
        "schema": "drycontroller.host-release.v1",
        "release_id": release_id,
        "sequence": sequence,
        "version": f"{sequence}.0.0",
        "source_commit": f"{sequence:x}" * 40,
        "registry": REGISTRY,
        "signing_key_sha256": f"sha256:{hashlib.sha256(HOST_KEY).hexdigest()}",
        "cosign": COSIGN_TOOLCHAIN,
        "bundle": {
            "image": f"{REGISTRY}/drycontroller/host-bundle@sha256:{sequence + 10:064x}",
            "image_id": f"sha256:{hashlib.sha256(bundle_config).hexdigest()}",
        },
        "topology": topology,
        "topology_digest": canonical_topology_digest(topology),
        "software_release_id": software["release_id"],
        "software_manifest_digest": canonical_digest(software),
        "files": [
            {
                "path": path,
                "sha256": f"sha256:{hashlib.sha256(data).hexdigest()}",
                "size": len(data),
                "mode": mode,
            }
            for path, (data, mode) in sorted(files.items())
        ],
    }


def _bundle(path: Path, files: dict[str, tuple[bytes, int]], config: bytes) -> None:
    layer_buffer = io.BytesIO()
    with tarfile.open(fileobj=layer_buffer, mode="w") as layer:
        for relative, (data, mode) in sorted(files.items()):
            info = tarfile.TarInfo(f"host-release/{relative}")
            info.size = len(data)
            info.mode = mode
            info.type = tarfile.REGTYPE
            layer.addfile(info, io.BytesIO(data))
    layer_bytes = layer_buffer.getvalue()
    image_manifest = json.dumps(
        [{"Config": "config.json", "RepoTags": ["bundle:test"], "Layers": ["layer.tar"]}],
        separators=(",", ":"),
    ).encode()
    with tarfile.open(path, "w") as archive:
        for name, data in (
            ("manifest.json", image_manifest),
            ("config.json", config),
            ("layer.tar", layer_bytes),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def _image_archive(path: Path, references: list[str], configs: dict[str, bytes]) -> None:
    records = []
    with tarfile.open(path, "w") as archive:
        for index, reference in enumerate(references):
            config_name = f"config-{index}.json"
            config = configs[reference]
            records.append({"Config": config_name, "RepoTags": [reference]})
            info = tarfile.TarInfo(config_name)
            info.size = len(config)
            archive.addfile(info, io.BytesIO(config))
        manifest = json.dumps(records, separators=(",", ":")).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))


def _paths(tmp_path: Path) -> HostReleasePaths:
    for directory in ("host-releases", "software-releases", "keys", "registry", "state"):
        (tmp_path / directory).mkdir()
    (tmp_path / "keys" / "host.pub").write_bytes(HOST_KEY)
    (tmp_path / "keys" / "software.pub").write_bytes(SOFTWARE_KEY)
    (tmp_path / "registry" / "status.json").write_text(
        json.dumps({"status": "active", "registry_host": REGISTRY}),
        encoding="utf-8",
    )
    (tmp_path / "state" / "device.json").write_text(
        json.dumps(
            {
                "schema": "drycontroller.device-identity.v1",
                "tenant_id": "tenant-a",
                "line_id": "line-a",
                "device_id": "EDGE-001",
                "node_name": "edge-001",
                "runtime_credential": "credential",
                "runtime_credential_hash": hashlib.sha256(b"credential").hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "state" / "cloud.env").write_text(
        "CLOUD_OPS_BASE_URL=https://ops.example.test\n",
        encoding="utf-8",
    )
    (tmp_path / "state" / "runtime.env").write_text("TEST=1\n", encoding="utf-8")
    paths = HostReleasePaths(
        lock=tmp_path / "state" / "release.lock",
        journal=tmp_path / "state" / "coordinator.json",
        request=tmp_path / "state" / "request.json",
        releases=tmp_path / "host-releases",
        current_link=tmp_path / "current",
        compatibility_link=tmp_path / "compatibility",
        host_active=tmp_path / "state" / "host-active.json",
        software_journal=tmp_path / "state" / "software-journal.json",
        software_releases=tmp_path / "software-releases",
        host_cosign_pubkey=tmp_path / "keys" / "host.pub",
        software_cosign_pubkey=tmp_path / "keys" / "software.pub",
        registry_status=tmp_path / "registry" / "status.json",
        device=tmp_path / "state" / "device.json",
        cloud_config=tmp_path / "state" / "cloud.env",
        runtime_env=tmp_path / "state" / "runtime.env",
    )
    paths.compatibility_link.symlink_to(paths.current_link)
    return paths


def _configure_root_docker_auth(tmp_path: Path, monkeypatch, payload: object | None = None) -> Path:
    root = tmp_path / "root-docker-state"
    root.mkdir(mode=0o700)
    source = root / "registry-auth.json"
    source.write_text(json.dumps({"auths": {}} if payload is None else payload), encoding="utf-8")
    source.chmod(0o400)
    monkeypatch.setattr(host_release_engine, "_REGISTRY_AUTH_SOURCE", source)
    monkeypatch.setattr(host_release_engine, "_DOCKER_CONFIG_PARENT", root)
    return source


def _install_slot(
    paths: HostReleasePaths,
    host: dict,
    software: dict,
    files: dict[str, tuple[bytes, int]],
    image_config: bytes,
) -> dict:
    root = paths.releases / host["release_id"]
    for relative, (data, mode) in files.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        destination.chmod(mode)
    (root / ".host-release-manifest.json").write_text(
        json.dumps(host, separators=(",", ":")),
        encoding="utf-8",
    )
    (root / ".host-release-signature.txt").write_text(SIGNATURE, encoding="utf-8")
    software_digest = canonical_digest(software)
    local = _local_image_reference("edge-api", software["image_ids"]["edge-api"])
    evidence_dir = paths.software_releases / software_digest.removeprefix("sha256:")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "local-images.json").write_text(
        json.dumps(
            {
                "manifest_digest": software_digest,
                "images": {
                    "edge-api": {
                        "reference": local,
                        "image_id": software["image_ids"]["edge-api"],
                        "runtime_id": software["image_ids"]["edge-api"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return {
        "host_manifest": host,
        "host_manifest_digest": canonical_digest(host),
        "host_manifest_signature": SIGNATURE,
        "software_manifest": software,
        "software_manifest_digest": software_digest,
        "software_manifest_signature": SIGNATURE,
        "host_path": str(root),
    }


class FakeCommands:
    def __init__(
        self,
        *,
        bundle_archive: Path,
        host: dict,
        software_configs: dict[str, bytes],
        fail_candidate_start: bool = False,
    ) -> None:
        self.bundle_archive = bundle_archive
        self.host = host
        self.software_configs = software_configs
        self.fail_candidate_start = fail_candidate_start
        self.commands: list[list[str]] = []
        self.compose_volumes: list[dict] = []
        self.compose_project_volumes: dict[str, dict] = {}
        self.compose_configs: dict[str, dict] = {}
        self.runtime_id_overrides: dict[str, str] = {}

    def __call__(self, args: list[str], cwd: Path | None, _input: bytes | None) -> str:
        self.commands.append(args)
        if args[:3] == ["/usr/bin/docker", "image", "inspect"]:
            reference = args[-1]
            if reference in self.runtime_id_overrides:
                return self.runtime_id_overrides[reference] + "\n"
            if reference == self.host["bundle"]["image"]:
                return self.host["bundle"]["image_id"] + "\n"
            if reference in self.software_configs:
                return f"sha256:{hashlib.sha256(self.software_configs[reference]).hexdigest()}\n"
        if args[:4] == ["/usr/bin/docker", "image", "save", "-o"]:
            destination = Path(args[4])
            references = args[5:]
            if references == [self.host["bundle"]["image"]]:
                shutil.copyfile(self.bundle_archive, destination)
            else:
                _image_archive(destination, references, self.software_configs)
            return ""
        if args[:2] == [host_release_engine._DOCKER_COMPOSE, "--env-file"] and "config" in args:
            override = Path(args[args.index("--file", args.index("--file") + 1) + 1])
            image_line = next(
                line.strip().removeprefix("image: ")
                for line in override.read_text(encoding="utf-8").splitlines()
                if line.strip().startswith("image: ")
            )
            image = json.loads(image_line)
            config = {
                "services": {
                    "edge-api": {
                        "image": image,
                        "volumes": self.compose_volumes,
                    }
                }
            }
            if self.compose_project_volumes:
                config["volumes"] = self.compose_project_volumes
            if self.compose_configs:
                config["configs"] = self.compose_configs
            return json.dumps(config)
        if args and args[0].endswith("start-active-release") and self.fail_candidate_start:
            if cwd == Path(self.host_path):
                self.fail_candidate_start = False
                raise HostReleaseError("candidate unhealthy")
        return ""

    host_path: str


def _policy(operation_id: str, host: dict, software: dict) -> dict:
    return {
        "state": "authorized",
        "operation_id": operation_id,
        "tenant_id": "tenant-a",
        "line_id": "line-a",
        "device_id": "EDGE-001",
        "node_name": "edge-001",
        "node_id": int(operation_id[16:], 16),
        "host_release_id": host["release_id"],
        "host_manifest_digest": canonical_digest(host),
        "host_manifest_signature": SIGNATURE,
        "host_manifest": host,
        "software_release_id": software["release_id"],
        "software_manifest_digest": canonical_digest(software),
        "software_manifest_signature": SIGNATURE,
        "software_manifest": software,
    }


def _fixture(tmp_path: Path, *, fail_candidate_start: bool = False):
    paths = _paths(tmp_path)
    host1_id = "11111111-1111-4111-8111-111111111111"
    software1_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    files1 = _files(1)
    topology1 = {
        "compose_digest": f"sha256:{hashlib.sha256(files1['docker-compose.yml'][0]).hexdigest()}",
        "service_inventory": ["edge-api"],
        "health_contract": HOST_HEALTH_CONTRACT,
    }
    software1, config1 = _software(software1_id, host1_id, canonical_topology_digest(topology1), 1)
    bundle_config1 = b'{"architecture":"amd64","sequence":1}'
    host1 = _host(host1_id, software1, 1, files1, bundle_config1)
    active = _install_slot(paths, host1, software1, files1, config1)

    host2_id = "22222222-2222-4222-8222-222222222222"
    software2_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    files2 = _files(2)
    topology2 = {
        "compose_digest": f"sha256:{hashlib.sha256(files2['docker-compose.yml'][0]).hexdigest()}",
        "service_inventory": ["edge-api"],
        "health_contract": HOST_HEALTH_CONTRACT,
    }
    software2, config2 = _software(software2_id, host2_id, canonical_topology_digest(topology2), 2)
    bundle_config2 = b'{"architecture":"amd64","sequence":2}'
    host2 = _host(host2_id, software2, 2, files2, bundle_config2)
    archive = tmp_path / "candidate-bundle.tar"
    _bundle(archive, files2, bundle_config2)
    command = FakeCommands(
        bundle_archive=archive,
        host=host2,
        software_configs={
            _local_image_reference("edge-api", software1["image_ids"]["edge-api"]): config1,
            _local_image_reference("edge-api", software2["image_ids"]["edge-api"]): config2,
        },
        fail_candidate_start=fail_candidate_start,
    )
    command.host_path = str(paths.releases / host2_id)

    (paths.request).write_text(json.dumps({"operation_id": "00000000000000020000000000000007"}), encoding="utf-8")
    paths.journal.write_text(
        json.dumps(
            {
                "schema": COORDINATOR_JOURNAL_SCHEMA,
                "phase": "idle",
                "operation_id": None,
                "last_operation_id": None,
                "high_watermark": 1,
                "active": active,
                "candidate": None,
                "previous": None,
                "effect": None,
                "error": None,
            }
        ),
        encoding="utf-8",
    )
    paths.software_journal.write_text(
        json.dumps(
            {
                "schema": "drycontroller.release-journal.v1",
                "phase": "idle",
                "operation_id": None,
                "last_operation_id": None,
                "active": {
                    "manifest": software1,
                    "manifest_digest": canonical_digest(software1),
                    "manifest_signature": SIGNATURE,
                    "verified_at": "2026-08-13T00:00:00Z",
                },
                "candidate": None,
                "previous": None,
                "effect": None,
                "error": None,
            }
        ),
        encoding="utf-8",
    )
    paths.host_active.write_text(
        json.dumps(
            {
                "schema": "drycontroller.host-active.v1",
                "host_release_id": host1_id,
                "host_release_manifest_digest": canonical_digest(host1),
                "topology_digest": host1["topology_digest"],
                "compose_digest": host1["topology"]["compose_digest"],
                "service_inventory": ["edge-api"],
            }
        ),
        encoding="utf-8",
    )
    return paths, host1, software1, host2, software2, command


def test_manifest_rejects_runner_self_update_and_special_modes(tmp_path):
    paths, _host1, _software1, host2, _software2, _command = _fixture(tmp_path)
    assert validate_host_manifest(host2) == host2

    runner = {
        **host2,
        "files": [
            *host2["files"],
            {
                "path": "infra/host-release-runner/host_release_runner.py",
                "sha256": "sha256:" + "0" * 64,
                "size": 1,
                "mode": 0o644,
            },
        ],
    }
    with pytest.raises(HostReleaseError, match="outside the fixed allowlist"):
        validate_host_manifest(runner)

    setuid = {**host2, "files": [dict(item) for item in host2["files"]]}
    setuid["files"][0]["mode"] = 0o4755
    with pytest.raises(HostReleaseError, match="mode invalid"):
        validate_host_manifest(setuid)

    trust_root = {**host2, "files": [dict(item) for item in host2["files"]]}
    trust_root["files"].append(
        {
            "path": "configs/keys/cosign.pub",
            "sha256": "sha256:" + "0" * 64,
            "size": 1,
            "mode": 0o644,
        }
    )
    with pytest.raises(HostReleaseError, match="may not contain trust roots"):
        validate_host_manifest(trust_root)

    for path in ("configs/a//b", "configs/a/b/", "configs//a"):
        noncanonical = {**host2, "files": [dict(item) for item in host2["files"]]}
        noncanonical["files"].append(
            {
                "path": path,
                "sha256": "sha256:" + "0" * 64,
                "size": 1,
                "mode": 0o644,
            }
        )
        with pytest.raises(HostReleaseError, match="path invalid"):
            validate_host_manifest(noncanonical)

    assert paths.releases.is_dir()


def test_paired_host_release_promotes_one_h_t_s_tuple(tmp_path):
    paths, host1, _software1, host2, software2, command = _fixture(tmp_path)
    operation_id = "00000000000000020000000000000007"
    engine = HostReleaseEngine(
        paths=paths,
        command_runner=command,
        policy_fetcher=lambda value: _policy(value, host2, software2),
    )

    result = engine.apply()

    assert result == {
        "operation_id": operation_id,
        "host_release_id": host2["release_id"],
        "software_release_id": software2["release_id"],
        "status": "succeeded",
    }
    journal = json.loads(paths.journal.read_text(encoding="utf-8"))
    assert journal["active"]["host_manifest"]["release_id"] == host2["release_id"]
    assert journal["active"]["software_manifest"]["release_id"] == software2["release_id"]
    assert journal["previous"]["host_manifest"]["release_id"] == host1["release_id"]
    assert journal["high_watermark"] == 2
    assert paths.current_link.resolve() == paths.releases / host2["release_id"]
    assert paths.compatibility_link.resolve() == paths.releases / host2["release_id"]
    active = json.loads(paths.host_active.read_text(encoding="utf-8"))
    assert active["host_release_id"] == host2["release_id"]
    software_journal = json.loads(paths.software_journal.read_text(encoding="utf-8"))
    assert software_journal["active"]["manifest"]["release_id"] == software2["release_id"]


def _blank_initial_fixture(tmp_path: Path):
    paths = _paths(tmp_path)
    host_id = "11111111-1111-4111-8111-111111111111"
    software_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    files = _files(1)
    topology = {
        "compose_digest": f"sha256:{hashlib.sha256(files['docker-compose.yml'][0]).hexdigest()}",
        "service_inventory": ["edge-api"],
        "health_contract": HOST_HEALTH_CONTRACT,
    }
    software, config = _software(
        software_id,
        host_id,
        canonical_topology_digest(topology),
        1,
    )
    bundle_config = b'{"architecture":"amd64","sequence":1}'
    host = _host(host_id, software, 1, files, bundle_config)
    host_archive = tmp_path / "initial-host.tar"
    _bundle(host_archive, files, bundle_config)
    local_reference = _local_image_reference("edge-api", software["image_ids"]["edge-api"])
    software_archive = tmp_path / "initial-software.tar"
    _image_archive(software_archive, [local_reference], {local_reference: config})
    command = FakeCommands(
        bundle_archive=host_archive,
        host=host,
        software_configs={local_reference: config},
    )
    command.host_path = str(paths.releases / host_id)
    return paths, host, software, host_archive, software_archive, command


def test_initial_pair_is_verified_started_and_idempotent(tmp_path):
    paths, host, software, host_archive, software_archive, command = _blank_initial_fixture(
        tmp_path
    )
    engine = HostReleaseEngine(paths=paths, command_runner=command)
    host_seed = {
        "manifest": host,
        "manifest_digest": canonical_digest(host),
        "manifest_signature": SIGNATURE,
    }
    software_seed = {
        "manifest": software,
        "manifest_digest": canonical_digest(software),
        "manifest_signature": SIGNATURE,
    }

    result = engine.initialize_initial_pair(
        host_seed=host_seed,
        software_seed=software_seed,
        host_image_archive=host_archive,
        software_image_archive=software_archive,
    )

    assert result == {
        "host_release_id": host["release_id"],
        "software_release_id": software["release_id"],
        "status": "initialized",
    }
    journal = json.loads(paths.journal.read_text(encoding="utf-8"))
    assert journal["phase"] == "idle"
    assert journal["active"]["host_manifest"] == host
    assert journal["active"]["software_manifest"] == software
    assert journal["candidate"] is None
    assert journal["previous"] is None
    assert paths.current_link.resolve() == paths.releases / host["release_id"]
    assert paths.compatibility_link.readlink() == paths.current_link
    assert json.loads(paths.host_active.read_text(encoding="utf-8"))["host_release_id"] == host[
        "release_id"
    ]
    assert json.loads(paths.software_journal.read_text(encoding="utf-8"))["active"][
        "manifest"
    ] == software
    assert any(args[:4] == ["/usr/bin/docker", "image", "load", "-i"] for args in command.commands)
    assert any(args and args[0].endswith("start-active-release") for args in command.commands)

    assert engine.initialize_initial_pair(
        host_seed=host_seed,
        software_seed=software_seed,
        host_image_archive=host_archive,
        software_image_archive=software_archive,
    )["status"] == "already_initialized"


def test_initial_pair_guard_and_start_share_one_held_transaction_lock(tmp_path):
    paths, host, software, host_archive, software_archive, command = _blank_initial_fixture(
        tmp_path
    )
    events: list[tuple[str, int]] = []

    def locked_command(args, cwd, input_bytes):
        if len(args) > 1 and args[1].endswith("assert-fresh-install-data-plane"):
            guard_fd = int(args[5])
            probe = os.open(paths.lock, os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
            events.append(("guard", guard_fd))
        elif args and args[0].endswith("start-active-release"):
            events.append(("start", int(args[2])))
        return command(args, cwd, input_bytes)

    HostReleaseEngine(paths=paths, command_runner=locked_command).initialize_initial_pair(
        host_seed={
            "manifest": host,
            "manifest_digest": canonical_digest(host),
            "manifest_signature": SIGNATURE,
        },
        software_seed={
            "manifest": software,
            "manifest_digest": canonical_digest(software),
            "manifest_signature": SIGNATURE,
        },
        host_image_archive=host_archive,
        software_image_archive=software_archive,
    )

    assert [name for name, _descriptor in events] == ["guard", "start"]
    assert events[0][1] == events[1][1]
    guard_index = next(
        index
        for index, args in enumerate(command.commands)
        if len(args) > 1 and args[1].endswith("assert-fresh-install-data-plane")
    )
    image_load_index = next(
        index
        for index, args in enumerate(command.commands)
        if args[:4] == ["/usr/bin/docker", "image", "load", "-i"]
    )
    assert guard_index < image_load_index


def test_initial_pair_guard_refusal_prevents_docker_and_state_mutation(tmp_path):
    paths, host, software, host_archive, software_archive, command = _blank_initial_fixture(
        tmp_path
    )

    def refuse_guard(args, cwd, input_bytes):
        if len(args) > 1 and args[1].endswith("assert-fresh-install-data-plane"):
            raise HostReleaseError("fresh-install guard refused")
        return command(args, cwd, input_bytes)

    with pytest.raises(HostReleaseError, match="fresh-install guard refused"):
        HostReleaseEngine(paths=paths, command_runner=refuse_guard).initialize_initial_pair(
            host_seed={
                "manifest": host,
                "manifest_digest": canonical_digest(host),
                "manifest_signature": SIGNATURE,
            },
            software_seed={
                "manifest": software,
                "manifest_digest": canonical_digest(software),
                "manifest_signature": SIGNATURE,
            },
            host_image_archive=host_archive,
            software_image_archive=software_archive,
        )

    assert not any(args and args[0] == "/usr/bin/docker" for args in command.commands)
    assert not paths.journal.exists()
    assert not paths.current_link.exists()


def test_initial_pair_archive_tamper_leaves_no_active_state(tmp_path):
    paths, host, software, host_archive, software_archive, command = _blank_initial_fixture(
        tmp_path
    )
    tampered_files = _files(1)
    tampered_files["configs/tags.yaml"] = (b"unsigned\n", 0o644)
    _bundle(host_archive, tampered_files, b'{"architecture":"amd64","sequence":1}')
    engine = HostReleaseEngine(paths=paths, command_runner=command)

    with pytest.raises(HostReleaseError, match="initial H1/T1/S1 activation failed"):
        engine.initialize_initial_pair(
            host_seed={
                "manifest": host,
                "manifest_digest": canonical_digest(host),
                "manifest_signature": SIGNATURE,
            },
            software_seed={
                "manifest": software,
                "manifest_digest": canonical_digest(software),
                "manifest_signature": SIGNATURE,
            },
            host_image_archive=host_archive,
            software_image_archive=software_archive,
        )

    assert not paths.journal.exists()
    assert not paths.host_active.exists()
    assert not paths.software_journal.exists()
    assert not paths.current_link.exists()
    assert not (paths.releases / host["release_id"]).exists()


def test_initial_pair_rejects_mismatched_host_and_software_before_mutation(tmp_path):
    paths, host, software, host_archive, software_archive, command = _blank_initial_fixture(
        tmp_path
    )
    mismatched = {
        **software,
        "host_release_id": "22222222-2222-4222-8222-222222222222",
    }
    engine = HostReleaseEngine(paths=paths, command_runner=command)

    with pytest.raises(HostReleaseError, match="pair mismatch"):
        engine.initialize_initial_pair(
            host_seed={
                "manifest": host,
                "manifest_digest": canonical_digest(host),
                "manifest_signature": SIGNATURE,
            },
            software_seed={
                "manifest": mismatched,
                "manifest_digest": canonical_digest(mismatched),
                "manifest_signature": SIGNATURE,
            },
            host_image_archive=host_archive,
            software_image_archive=software_archive,
        )

    assert not paths.journal.exists()
    assert not paths.current_link.exists()


def test_interrupted_initial_pair_is_cleaned_and_retried_exactly(tmp_path):
    paths, host, software, host_archive, software_archive, command = _blank_initial_fixture(
        tmp_path
    )
    engine = HostReleaseEngine(paths=paths, command_runner=command)
    host_seed = {
        "manifest": host,
        "manifest_digest": canonical_digest(host),
        "manifest_signature": SIGNATURE,
    }
    software_seed = {
        "manifest": software,
        "manifest_digest": canonical_digest(software),
        "manifest_signature": SIGNATURE,
    }
    slot = engine._initial_slot(
        host=host,
        host_digest=host_seed["manifest_digest"],
        host_signature=SIGNATURE,
        software=software,
        software_digest=software_seed["manifest_digest"],
        software_signature=SIGNATURE,
    )
    interrupted_root = Path(slot["host_path"])
    interrupted_root.mkdir()
    paths.current_link.symlink_to(interrupted_root)
    paths.host_active.write_text("{}", encoding="utf-8")
    paths.software_journal.write_text("{}", encoding="utf-8")
    interrupted_software = paths.software_releases / software_seed[
        "manifest_digest"
    ].removeprefix("sha256:")
    interrupted_software.mkdir()
    paths.journal.write_text(
        json.dumps(engine._initial_journal(slot, "initial_activating")),
        encoding="utf-8",
    )

    result = engine.initialize_initial_pair(
        host_seed=host_seed,
        software_seed=software_seed,
        host_image_archive=host_archive,
        software_image_archive=software_archive,
    )

    assert result["status"] == "initialized"
    assert paths.current_link.resolve() == interrupted_root
    assert json.loads(paths.journal.read_text(encoding="utf-8"))["phase"] == "idle"
    assert any(
        args[:2] == [host_release_engine._DOCKER_COMPOSE, "--env-file"] and "down" in args
        for args in command.commands
    )


def test_portable_image_identity_is_independent_of_containerd_runtime_id(tmp_path):
    paths, _host1, _software1, host2, software2, command = _fixture(tmp_path)
    local_reference = _local_image_reference("edge-api", software2["image_ids"]["edge-api"])
    command.runtime_id_overrides = {
        host2["bundle"]["image"]: "sha256:" + "e" * 64,
        local_reference: "sha256:" + "f" * 64,
    }
    engine = HostReleaseEngine(
        paths=paths,
        command_runner=command,
        policy_fetcher=lambda value: _policy(value, host2, software2),
    )

    assert engine.apply()["status"] == "succeeded"

    evidence = json.loads(
        (
            paths.software_releases
            / canonical_digest(software2).removeprefix("sha256:")
            / "local-images.json"
        ).read_text(encoding="utf-8")
    )
    assert evidence["images"]["edge-api"]["image_id"] == software2["image_ids"]["edge-api"]
    assert evidence["images"]["edge-api"]["runtime_id"] == "sha256:" + "f" * 64


def test_candidate_intent_is_durable_before_staging_effects(tmp_path):
    paths, _host1, _software1, host2, software2, command = _fixture(tmp_path)
    engine = HostReleaseEngine(
        paths=paths,
        command_runner=command,
        policy_fetcher=lambda value: _policy(value, host2, software2),
    )
    observed: list[dict] = []

    def fail_after_reading_intent(_journal, _policy):
        observed.append(json.loads(paths.journal.read_text(encoding="utf-8")))
        raise HostReleaseError("staging interrupted")

    engine._stage_candidate = fail_after_reading_intent  # type: ignore[method-assign]

    with pytest.raises(HostReleaseError, match="staging interrupted"):
        engine.apply()

    assert observed[0]["phase"] == "candidate_staging"
    assert observed[0]["candidate"]["host_manifest"]["release_id"] == host2["release_id"]
    assert observed[0]["candidate"]["software_manifest"]["release_id"] == software2["release_id"]


def test_compose_bind_policy_keeps_signed_files_and_trust_roots_read_only(tmp_path):
    paths, host1, software1, _host2, _software2, command = _fixture(tmp_path)
    engine = HostReleaseEngine(paths=paths, command_runner=command, policy_fetcher=lambda _value: {})
    rootfs = paths.releases / host1["release_id"]

    command.compose_volumes = [
        {"type": "bind", "source": "/run/drycontroller-host-release", "read_only": True}
    ]
    engine._validate_compose(host1, software1, rootfs)

    command.compose_volumes = [
        {"type": "bind", "source": "/etc/drycontroller/keys", "read_only": False}
    ]
    with pytest.raises(HostReleaseError, match="host bind path forbidden"):
        engine._validate_compose(host1, software1, rootfs)

    command.compose_volumes = [
        {"type": "bind", "source": str(rootfs / "configs"), "read_only": False}
    ]
    with pytest.raises(HostReleaseError, match="must be mounted read-only"):
        engine._validate_compose(host1, software1, rootfs)


def test_compose_rejects_named_volume_driver_bind_and_external_files(tmp_path):
    paths, host1, software1, _host2, _software2, command = _fixture(tmp_path)
    engine = HostReleaseEngine(paths=paths, command_runner=command, policy_fetcher=lambda _value: {})
    rootfs = paths.releases / host1["release_id"]

    command.compose_volumes = [
        {"type": "volume", "source": "postgres_data", "target": "/var/lib/postgresql/data"}
    ]
    command.compose_project_volumes = {
        "postgres_data": {"name": "drycontroller-edge_postgres_data"}
    }
    engine._validate_compose(host1, software1, rootfs)

    command.compose_volumes = [
        {"type": "volume", "source": "host-root", "target": "/host", "read_only": False}
    ]
    command.compose_project_volumes = {
        "host-root": {
            "name": "host-root",
            "driver_opts": {"type": "none", "o": "bind", "device": "/"},
        }
    }
    with pytest.raises(HostReleaseError, match="named volume forbidden"):
        engine._validate_compose(host1, software1, rootfs)

    command.compose_volumes = []
    command.compose_project_volumes = {}
    command.compose_configs = {"host-secret": {"file": "/var/lib/drycontroller/device.json"}}
    with pytest.raises(HostReleaseError, match="external file injection forbidden"):
        engine._validate_compose(host1, software1, rootfs)


def test_compatibility_link_is_static_indirection_outside_mutable_parent(tmp_path):
    paths, _host1, _software1, host2, software2, command = _fixture(tmp_path)
    engine = HostReleaseEngine(
        paths=paths,
        command_runner=command,
        policy_fetcher=lambda value: _policy(value, host2, software2),
    )

    engine.apply()

    assert paths.compatibility_link.readlink() == paths.current_link
    assert paths.compatibility_link.resolve() == paths.releases / host2["release_id"]

    paths.compatibility_link.unlink()
    paths.compatibility_link.symlink_to(paths.releases / host2["release_id"])
    with pytest.raises(HostReleaseError, match="immutable indirection"):
        engine._atomic_links(paths.releases / host2["release_id"])


def test_candidate_health_failure_rolls_back_without_registry_access(tmp_path):
    paths, host1, software1, host2, software2, command = _fixture(
        tmp_path,
        fail_candidate_start=True,
    )
    engine = HostReleaseEngine(
        paths=paths,
        command_runner=command,
        policy_fetcher=lambda value: _policy(value, host2, software2),
    )

    with pytest.raises(HostReleaseError, match="candidate unhealthy"):
        engine.apply()

    journal = json.loads(paths.journal.read_text(encoding="utf-8"))
    assert journal["phase"] == "idle"
    assert journal["active"]["host_manifest"]["release_id"] == host1["release_id"]
    assert journal["active"]["software_manifest"]["release_id"] == software1["release_id"]
    assert journal["high_watermark"] == 1
    assert paths.current_link.resolve() == paths.releases / host1["release_id"]
    assert not (paths.releases / host2["release_id"]).exists()
    assert not (paths.software_releases / canonical_digest(software2).removeprefix("sha256:")).exists()
    previous_started = [
        index for index, args in enumerate(command.commands) if args and args[0].endswith("start-active-release")
    ][-1]
    assert not any(args[:2] == ["/usr/bin/docker", "pull"] for args in command.commands[previous_started:])

    retry_operation_id = "00000000000000030000000000000007"
    paths.request.write_text(json.dumps({"operation_id": retry_operation_id}), encoding="utf-8")
    retry = HostReleaseEngine(
        paths=paths,
        command_runner=command,
        policy_fetcher=lambda value: _policy(value, host2, software2),
    ).apply()
    assert retry["status"] == "succeeded"


def test_interrupted_mutation_recovers_previous_before_new_work(tmp_path):
    paths, host1, software1, host2, software2, command = _fixture(tmp_path)
    journal = json.loads(paths.journal.read_text(encoding="utf-8"))
    candidate = _install_slot(paths, host2, software2, _files(2), next(iter(command.software_configs.values())))
    journal.update(
        {
            "phase": "candidate_activating",
            "operation_id": "00000000000000020000000000000007",
            "candidate": candidate,
            "previous": journal["active"],
        }
    )
    paths.journal.write_text(json.dumps(journal), encoding="utf-8")
    engine = HostReleaseEngine(
        paths=paths,
        command_runner=command,
        policy_fetcher=lambda _value: pytest.fail("policy must not be fetched during recovery"),
    )

    with pytest.raises(HostReleaseError, match="interrupted_host_release_recovered"):
        engine.apply()

    recovered = json.loads(paths.journal.read_text(encoding="utf-8"))
    assert recovered["phase"] == "idle"
    assert recovered["active"]["host_manifest"]["release_id"] == host1["release_id"]
    assert recovered["active"]["software_manifest"]["release_id"] == software1["release_id"]


@pytest.mark.parametrize(
    "phase",
    [
        "candidate_staging",
        "candidate_verified",
        "previous_proven",
        "active_stopping",
        "candidate_activating",
        "promotion_started",
        "rollback_started",
    ],
)
def test_interrupted_phase_matrix_removes_durable_candidate_and_restores_previous(tmp_path, phase):
    paths, host1, software1, host2, software2, command = _fixture(tmp_path)
    candidate = _install_slot(paths, host2, software2, _files(2), next(iter(command.software_configs.values())))
    journal = json.loads(paths.journal.read_text(encoding="utf-8"))
    journal.update(
        {
            "phase": phase,
            "operation_id": "00000000000000020000000000000007",
            "candidate": candidate,
            "previous": journal["active"],
        }
    )
    paths.journal.write_text(json.dumps(journal), encoding="utf-8")
    engine = HostReleaseEngine(
        paths=paths,
        command_runner=command,
        policy_fetcher=lambda _value: pytest.fail("recovery must not fetch a new policy"),
    )

    with pytest.raises(HostReleaseError, match="interrupted_host_release_recovered"):
        engine.apply()

    recovered = json.loads(paths.journal.read_text(encoding="utf-8"))
    assert recovered["phase"] == "idle"
    assert recovered["active"]["host_manifest"]["release_id"] == host1["release_id"]
    assert recovered["active"]["software_manifest"]["release_id"] == software1["release_id"]
    assert not Path(candidate["host_path"]).exists()
    assert not (paths.software_releases / canonical_digest(software2).removeprefix("sha256:")).exists()


def test_tpm_signer_uses_fixed_identity_install_directory(tmp_path):
    paths, _host1, _software1, _host2, _software2, _command = _fixture(tmp_path)
    observed: list[tuple[list[str], Path | None, bytes | None]] = []
    challenge = {"challenge_id": "challenge-1"}

    def command(args, cwd, input_bytes):
        observed.append((args, cwd, input_bytes))
        return json.dumps({"challenge_id": "challenge-1", "signature": "signature"})

    signature = HostReleaseEngine(paths=paths, command_runner=command)._authorize_with_tpm(challenge)

    assert signature == "signature"
    assert observed[0][1] == Path("/opt/drycontroller-identity")


def test_start_helper_receives_the_held_transaction_lock_fd(tmp_path):
    paths = _paths(tmp_path)
    helper = tmp_path / "start-active-release"
    helper.write_text(
        "#!/usr/bin/python3\n"
        "import fcntl, sys\n"
        "fd = int(sys.argv[2])\n"
        "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "print('inherited')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    paths.lock.touch(mode=0o600)
    descriptor = os.open(paths.lock, os.O_WRONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        output = HostReleaseEngine(paths=paths)._run_command(
            [str(helper), "--lock-fd", str(descriptor)]
        )
    finally:
        os.close(descriptor)

    assert output.splitlines()[-1] == "inherited"


def test_fresh_install_guard_receives_the_held_transaction_lock_fd(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    bash = tmp_path / "trusted-bash"
    bash.write_text("#!/bin/sh\nexec /bin/sh \"$@\"\n", encoding="utf-8")
    bash.chmod(0o755)
    helper = tmp_path / "assert-fresh-install-data-plane"
    helper.write_text(
        "#!/bin/sh\n"
        "python3 - \"$4\" <<'PY'\n"
        "import fcntl, sys\n"
        "fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "print('inherited')\n"
        "PY\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    monkeypatch.setattr(host_release_engine, "_BASH", str(bash))
    monkeypatch.setattr(host_release_engine, "_FRESH_INSTALL_GUARD", str(helper))
    paths.lock.touch(mode=0o600)
    descriptor = os.open(paths.lock, os.O_WRONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        output = HostReleaseEngine(paths=paths)._run_command(
            [
                str(bash),
                str(helper),
                "--project",
                "drycontroller-edge",
                "--lock-fd",
                str(descriptor),
            ]
        )
    finally:
        os.close(descriptor)

    assert output.splitlines()[-1] == "inherited"


def test_host_docker_commands_require_one_fixed_trusted_executable(tmp_path, monkeypatch):
    _configure_root_docker_auth(tmp_path, monkeypatch)
    trusted_dir = tmp_path / "trusted" / "bin"
    trusted_dir.mkdir(parents=True)
    trusted = trusted_dir / "docker"
    trusted.write_text("#!/bin/sh\necho trusted\n", encoding="utf-8")
    trusted.chmod(0o755)
    monkeypatch.setattr(host_release_engine, "_DOCKER", str(trusted))
    engine = HostReleaseEngine(paths=_paths(tmp_path))

    assert engine._run_command([str(trusted), "version"]).strip() == "trusted"

    trusted.chmod(0o777)
    with pytest.raises(HostReleaseError, match="untrusted"):
        engine._run_command([str(trusted), "version"])
    trusted.chmod(0o755)

    symlink = trusted_dir / "docker-link"
    symlink.symlink_to(trusted)
    monkeypatch.setattr(host_release_engine, "_DOCKER", str(symlink))
    with pytest.raises(HostReleaseError, match="untrusted"):
        engine._run_command([str(symlink), "version"])

    drift = tmp_path / "path-drift" / "docker"
    drift.parent.mkdir()
    drift.write_text("#!/bin/sh\necho drift\n", encoding="utf-8")
    drift.chmod(0o755)
    monkeypatch.setattr(host_release_engine, "_DOCKER", str(trusted))
    with pytest.raises(HostReleaseError, match="path drift"):
        engine._run_command([str(drift), "version"])


@pytest.mark.parametrize(
    "payload",
    [
        {"auths": {}, "currentContext": "attacker"},
        {"auths": {}, "cli-plugins": {"extraDirs": ["/tmp/plugins"]}},
        {"auths": {}, "plugins": {}},
        {"auths": {}, "proxies": {}},
        {"auths": {}, "credsStore": "attacker"},
        {"auths": {}, "credHelpers": {"registry.example.test": "attacker"}},
        {"auths": {"registry.example.test": {"credHelpers": "attacker"}}},
        {"auths": {"registry.example.test": {"auth": "not-base64"}}},
    ],
)
def test_host_root_docker_auth_source_rejects_plugin_context_helpers_and_unknown_schema(
    tmp_path,
    monkeypatch,
    payload,
):
    _configure_root_docker_auth(tmp_path, monkeypatch, payload)
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    docker.chmod(0o755)
    monkeypatch.setattr(host_release_engine, "_DOCKER", str(docker))
    fixture = tmp_path / "fixture"
    fixture.mkdir()

    with pytest.raises(HostReleaseError, match="schema"):
        HostReleaseEngine(paths=_paths(fixture))._run_command([str(docker), "version"])


@pytest.mark.parametrize(
    "raw",
    [
        '{"auths":{},"auths":{"registry.example.test":{"auth":"dXNlcjpwYXNz"}}}',
        '{"auths":{"registry.example.test":{"auth":"dXNlcjpwYXNz","identitytoken":"bad"}}}',
        "not-json",
    ],
)
def test_host_root_docker_auth_source_rejects_duplicate_nested_and_malformed_json(tmp_path, monkeypatch, raw):
    source = _configure_root_docker_auth(tmp_path, monkeypatch)
    source.chmod(0o600)
    source.write_text(raw, encoding="utf-8")
    source.chmod(0o400)

    with pytest.raises(HostReleaseError, match="root registry auth source"):
        host_release_engine._read_registry_auth_source()


def test_host_root_docker_auth_source_must_be_candidate_unwritable_metadata(tmp_path, monkeypatch):
    source = _configure_root_docker_auth(tmp_path, monkeypatch)
    source.chmod(0o600)

    with pytest.raises(HostReleaseError, match="root registry auth source"):
        host_release_engine._read_registry_auth_source()


def test_host_docker_command_gets_only_private_sanitized_auth_and_fixed_daemon(tmp_path, monkeypatch):
    auth = base64.b64encode(b"reader:secret").decode("ascii")
    _configure_root_docker_auth(
        tmp_path,
        monkeypatch,
        {"auths": {REGISTRY: {"auth": auth}}},
    )
    with host_release_engine._private_docker_environment() as environment:
        config_directory = Path(environment["DOCKER_CONFIG"])
        config = config_directory / "config.json"
        assert stat.S_IMODE(config_directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
        assert set(environment) == {"HOME", "PATH", "DOCKER_HOST", "DOCKER_CONFIG", "COMPOSE_PROJECT_NAME"}
    assert not config_directory.exists()
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$DOCKER_HOST\"\n"
        "printf '%s\\n' \"$DOCKER_CONFIG\"\n"
        "cat \"$DOCKER_CONFIG/config.json\"\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    monkeypatch.setattr(host_release_engine, "_DOCKER", str(docker))
    fixture = tmp_path / "fixture"
    fixture.mkdir()

    output = HostReleaseEngine(paths=_paths(fixture))._run_command([str(docker), "version"])
    lines = output.splitlines()
    temporary = Path(lines[1])

    assert lines[0] == "unix:///var/run/docker.sock"
    assert json.loads(lines[2]) == {"auths": {REGISTRY: {"auth": auth}}}
    assert not temporary.exists()


def test_command_output_is_bounded_before_it_enters_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(host_release_engine, "MAX_COMMAND_OUTPUT_BYTES", 128)
    producer = tmp_path / "large-output"
    producer.write_text("#!/bin/sh\nprintf '%0256d' 0\n", encoding="utf-8")
    producer.chmod(0o755)

    with pytest.raises(HostReleaseError, match="command output too large"):
        HostReleaseEngine(paths=_paths(tmp_path))._run_command([str(producer)])


def test_command_timeout_terminates_the_complete_process_group(tmp_path, monkeypatch):
    monkeypatch.setattr(host_release_engine, "COMMAND_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(host_release_engine, "COMMAND_TERMINATE_GRACE_SECONDS", 0.05)
    descendant_marker = tmp_path / "descendant"
    producer = tmp_path / "timeout-with-descendant"
    producer.write_text(
        f"#!/bin/sh\n(exec >/dev/null 2>&1; trap '' TERM; sleep 0.5; printf x > '{descendant_marker}') &\nwait\n",
        encoding="utf-8",
    )
    producer.chmod(0o755)

    with pytest.raises(HostReleaseError, match="command unavailable"):
        HostReleaseEngine(paths=_paths(tmp_path))._run_command([str(producer)])

    time.sleep(0.6)
    assert not descendant_marker.exists()


def test_failed_command_terminates_stdout_closed_descendants(tmp_path, monkeypatch):
    monkeypatch.setattr(host_release_engine, "COMMAND_TERMINATE_GRACE_SECONDS", 0.05)
    descendant_ready = tmp_path / "descendant-ready"
    descendant_marker = tmp_path / "descendant"
    producer = tmp_path / "failure-with-descendant"
    producer.write_text(
        "#!/bin/sh\n"
        f"(exec >/dev/null 2>&1; trap '' TERM; printf x > '{descendant_ready}'; "
        f"sleep 0.5; printf x > '{descendant_marker}') &\n"
        f"while [ ! -f '{descendant_ready}' ]; do sleep 0.01; done\n"
        "exit 1\n",
        encoding="utf-8",
    )
    producer.chmod(0o755)

    with pytest.raises(HostReleaseError, match="command failed"):
        HostReleaseEngine(paths=_paths(tmp_path))._run_command([str(producer)])

    assert descendant_ready.exists()
    time.sleep(0.6)
    assert not descendant_marker.exists()


def test_output_reader_start_failure_terminates_command(tmp_path, monkeypatch):
    monkeypatch.setattr(host_release_engine, "COMMAND_TERMINATE_GRACE_SECONDS", 0.05)
    marker = tmp_path / "command-survived"
    producer = tmp_path / "reader-start-failure"
    producer.write_text(f"#!/bin/sh\nsleep 0.3\nprintf x > '{marker}'\n", encoding="utf-8")
    producer.chmod(0o755)

    def fail_reader_start(_thread):
        raise RuntimeError("simulated thread exhaustion")

    monkeypatch.setattr(host_release_engine.threading.Thread, "start", fail_reader_start)

    with pytest.raises(HostReleaseError, match="command output unavailable"):
        HostReleaseEngine(paths=_paths(tmp_path))._run_command([str(producer)])

    time.sleep(0.4)
    assert not marker.exists()
