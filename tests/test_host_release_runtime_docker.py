from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER_DIR = ROOT / "infra" / "host-release-runner"
HELPER_DIR = ROOT / "infra" / "ota-helper"
for entry in (RUNNER_DIR, HELPER_DIR):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from host_release_engine import (  # noqa: E402
    COORDINATOR_JOURNAL_SCHEMA,
    COSIGN_TOOLCHAIN,
    HOST_ACTIVE_SCHEMA,
    HOST_HEALTH_CONTRACT,
    REGISTRY_AUTH_SCHEMA,
    HostReleaseEngine,
    HostReleaseError,
    HostReleasePaths,
    _local_image_reference,
    canonical_digest,
    canonical_json_bytes,
    canonical_topology_digest,
)
from release_engine import docker_archive_config_digest  # noqa: E402


HELPERS = {
    "infra/ota-helper/drycontroller_ota_helper.py": (b"# fixed helper\n", 0o644),
    "infra/ota-helper/release_engine.py": (b"# fixed engine\n", 0o644),
    "infra/ota-helper/ota-apply": (b"#!/usr/bin/python3\n", 0o755),
    "infra/ota-helper/initialize-active-release": (b"#!/usr/bin/python3\n", 0o755),
    "infra/ota-helper/start-active-release": (b"#!/usr/bin/python3\n", 0o755),
}


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> str:
    completed = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        check=False,
        capture_output=True,
        timeout=10 * 60,
    )
    if completed.returncode != 0:
        raise HostReleaseError(f"real command failed: {Path(args[0]).name} {args[1:3]}")
    return completed.stdout


def _sign_blob(
    cosign: str,
    manifest: dict,
    *,
    key: Path,
    password: str,
    signing_config: Path,
    output: Path,
) -> str:
    source = output.with_name(output.name + ".manifest.json")
    source.write_bytes(canonical_json_bytes(manifest))
    _run(
        [
            cosign,
            "sign-blob",
            "--yes",
            "--signing-config",
            str(signing_config),
            "--key",
            str(key),
            "--bundle",
            str(output),
            str(source),
        ],
        env={**os.environ, "COSIGN_PASSWORD": password},
    )
    return base64.b64encode(output.read_bytes()).decode("ascii")


def _push_and_sign(
    docker: str,
    cosign: str,
    *,
    local: str,
    target: str,
    key: Path,
    password: str,
    signing_config: Path,
) -> tuple[str, str]:
    _run([docker, "tag", local, target])
    _run([docker, "push", target])
    repository = target.rsplit(":", 1)[0]
    digests = _run(
        [docker, "image", "inspect", "--format", "{{range .RepoDigests}}{{println .}}{{end}}", target]
    ).splitlines()
    reference = next(value for value in digests if value.startswith(f"{repository}@sha256:"))
    descriptor, archive_name = tempfile.mkstemp(prefix="drycontroller-rb5c-image-", suffix=".tar")
    os.close(descriptor)
    archive = Path(archive_name)
    try:
        _run([docker, "image", "save", "-o", str(archive), target])
        image_id = docker_archive_config_digest(archive)
    finally:
        archive.unlink(missing_ok=True)
    _run(
        [
            cosign,
            "sign",
            "--yes",
            "--allow-insecure-registry",
            "--signing-config",
            str(signing_config),
            "--key",
            str(key),
            reference,
        ],
        env={**os.environ, "COSIGN_PASSWORD": password},
    )
    return reference, image_id


def _compose(services: tuple[str, ...]) -> bytes:
    lines = ["services:"]
    for service in services:
        lines.extend(
            [
                f"  {service}:",
                "    image: busybox:latest",
                '    command: ["sh", "-c", "sleep 300"]',
            ]
        )
    return ("\n".join(lines) + "\n").encode()


def _files(services: tuple[str, ...], version: str) -> dict[str, tuple[bytes, int]]:
    return {
        "docker-compose.yml": (_compose(services), 0o644),
        "configs/release.yaml": (f"version: {version}\n".encode(), 0o644),
        **HELPERS,
    }


def _bundle_tar(path: Path, files: dict[str, tuple[bytes, int]]) -> None:
    with tarfile.open(path, "w") as archive:
        for relative, (data, mode) in sorted(files.items()):
            member = tarfile.TarInfo(f"host-release/{relative}")
            member.size = len(data)
            member.mode = mode
            member.type = tarfile.REGTYPE
            archive.addfile(member, io.BytesIO(data))


def _host_manifest(
    *,
    release_id: str,
    sequence: int,
    registry: str,
    host_key: bytes,
    bundle_reference: str,
    bundle_image_id: str,
    software: dict,
    files: dict[str, tuple[bytes, int]],
) -> dict:
    compose = files["docker-compose.yml"][0]
    topology = {
        "compose_digest": "sha256:" + hashlib.sha256(compose).hexdigest(),
        "service_inventory": sorted(software["services"]),
        "health_contract": HOST_HEALTH_CONTRACT,
    }
    return {
        "schema": "drycontroller.host-release.v1",
        "release_id": release_id,
        "sequence": sequence,
        "version": f"{sequence}.0.0",
        "source_commit": f"{sequence:x}" * 40,
        "registry": registry,
        "signing_key_sha256": "sha256:" + hashlib.sha256(host_key).hexdigest(),
        "cosign": COSIGN_TOOLCHAIN,
        "bundle": {"image": bundle_reference, "image_id": bundle_image_id},
        "topology": topology,
        "topology_digest": canonical_topology_digest(topology),
        "software_release_id": software["release_id"],
        "software_manifest_digest": canonical_digest(software),
        "files": [
            {
                "path": relative,
                "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "mode": mode,
            }
            for relative, (data, mode) in sorted(files.items())
        ],
    }


@pytest.mark.skipif(
    os.environ.get("DRYCONTROLLER_RUN_DOCKER_HOST_RELEASE_E2E") != "1",
    reason="set DRYCONTROLLER_RUN_DOCKER_HOST_RELEASE_E2E=1 for isolated signed HostRelease E2E",
)
def test_signed_host_topology_change_and_failed_candidate_roll_back_offline(tmp_path: Path):
    docker = shutil.which("docker")
    cosign = shutil.which("cosign")
    if docker is None or cosign is None:
        pytest.skip("docker and cosign are required")
    for image in ("busybox:latest", "registry:2"):
        if subprocess.run([docker, "image", "inspect", image], capture_output=True).returncode != 0:
            pytest.skip(f"local image unavailable: {image}")
    projects = json.loads(_run([docker, "compose", "ls", "--all", "--format", "json"]))
    if any(project.get("Name") == "drycontroller-edge" for project in projects):
        pytest.fail("isolated Compose project drycontroller-edge is already in use")

    suffix = f"{os.getpid()}-{secrets.token_hex(6)}"
    registry_name = f"drycontroller-rb5c-registry-{suffix}"
    network_name = f"drycontroller-rb5c-network-{suffix}-{secrets.token_hex(6)}"
    registry_port = 56000 + os.getpid() % 1000
    registry = f"127.0.0.1:{registry_port}"
    signing_config = tmp_path / "signing-config.json"
    host_key_prefix = tmp_path / "host"
    software_key_prefix = tmp_path / "software"
    password = "rb5c-docker-e2e"
    created_images: set[str] = set()
    commands: list[list[str]] = []
    registry_offline_at: int | None = None
    network_created = False
    network_id: str | None = None

    _run(
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
        ]
    )
    for prefix in (host_key_prefix, software_key_prefix):
        _run(
            [cosign, "generate-key-pair", "--output-key-prefix", str(prefix)],
            env={**os.environ, "COSIGN_PASSWORD": password},
        )
    host_key = host_key_prefix.with_suffix(".key")
    host_pub = host_key_prefix.with_suffix(".pub")
    software_key = software_key_prefix.with_suffix(".key")
    software_pub = software_key_prefix.with_suffix(".pub")
    assert host_pub.read_bytes() != software_pub.read_bytes()

    paths = HostReleasePaths(
        lock=tmp_path / "state/release.lock",
        journal=tmp_path / "state/coordinator.json",
        request=tmp_path / "state/request.json",
        releases=tmp_path / "host-releases",
        current_link=tmp_path / "current",
        compatibility_link=tmp_path / "compatibility",
        host_active=tmp_path / "state/host-active.json",
        software_journal=tmp_path / "state/software-journal.json",
        software_releases=tmp_path / "software-releases",
        host_cosign_pubkey=host_pub,
        software_cosign_pubkey=software_pub,
        registry_status=tmp_path / "state/registry.json",
        device=tmp_path / "state/device.json",
        cloud_config=tmp_path / "state/cloud.env",
        runtime_env=tmp_path / "state/runtime.env",
    )
    for directory in (paths.releases, paths.software_releases, paths.journal.parent):
        directory.mkdir(parents=True, exist_ok=True)
    registry_secret = b"secret"
    paths.registry_status.write_text(
        json.dumps(
            {
                "schema": REGISTRY_AUTH_SCHEMA,
                "auths": {
                    registry: {
                        "auth": base64.b64encode(b"reader:" + registry_secret).decode("ascii")
                    }
                },
                "credential": {
                    "status": "active",
                    "credential_id": "cred-1",
                    "registry_host": registry,
                    "username": "reader",
                    "secret_fingerprint": hashlib.sha256(registry_secret).hexdigest()[:16],
                    "version": 2,
                    "transaction_id": "a" * 32,
                    "audit_id": 17,
                    "updated_at": "2026-08-19T00:00:00+00:00",
                },
            }
        )
    )
    paths.registry_status.chmod(0o400)
    paths.registry_status.parent.chmod(0o700)
    paths.device.write_text(
        json.dumps(
            {
                "schema": "drycontroller.device-identity.v1",
                "tenant_id": "tenant-a",
                "line_id": "line-a",
                "device_id": "EDGE-001",
                "node_name": "edge-001",
                "runtime_credential": "test-only",
                "runtime_credential_hash": hashlib.sha256(b"test-only").hexdigest(),
            }
        )
    )
    paths.cloud_config.write_text("CLOUD_OPS_BASE_URL=https://ops.example.test\n")
    paths.runtime_env.write_text("RB5C_E2E=1\n")

    releases: list[dict] = []

    def command_runner(args: list[str], cwd: Path | None, input_bytes: bytes | None) -> str:
        nonlocal registry_offline_at
        commands.append(args)
        if args[0] == "/usr/bin/systemctl":
            return ""
        if args[0].endswith("/infra/ota-helper/start-active-release"):
            software_journal = json.loads(paths.software_journal.read_text(encoding="utf-8"))
            software = software_journal["active"]["manifest"]
            override = tmp_path / f"{software['release_id']}.images.yml"
            lines = ["services:"]
            for service in sorted(software["services"]):
                lines.extend(
                    [
                        f"  {service}:",
                        f"    image: {json.dumps(_local_image_reference(service, software['image_ids'][service]))}",
                    ]
                )
            override.write_text("\n".join(lines) + "\n")
            compose_args = [
                docker,
                "compose",
                "--project-name",
                "drycontroller-edge",
                "--env-file",
                str(paths.runtime_env),
                "--file",
                str(cwd / "docker-compose.yml"),
                "--file",
                str(override),
                "up",
                "-d",
                "--remove-orphans",
                "--pull",
                "never",
                "--no-build",
            ]
            _run(compose_args, cwd=cwd)
            if software["version"] == "3.0.0":
                _run([docker, "stop", registry_name])
                registry_offline_at = len(commands)
                raise HostReleaseError("candidate health failed")
            output = _run(
                [
                    docker,
                    "compose",
                    "--project-name",
                    "drycontroller-edge",
                    "--file",
                    str(cwd / "docker-compose.yml"),
                    "--file",
                    str(override),
                    "ps",
                    "--format",
                    "json",
                ],
                cwd=cwd,
            )
            running = [json.loads(line) for line in output.splitlines() if line]
            if len(running) != len(software["services"]) or any(row.get("State") != "running" for row in running):
                raise HostReleaseError("candidate health failed")
            return ""
        if args[0] == "/usr/local/bin/cosign":
            actual = [cosign, *args[1:]]
            if len(actual) > 1 and actual[1] == "verify":
                actual.insert(2, "--allow-insecure-registry")
            return _run(actual, cwd=cwd)
        if args[0] in (
            "/usr/bin/docker",
            "/usr/libexec/docker/cli-plugins/docker-compose",
        ):
            subcommand = ["compose"] if Path(args[0]).name == "docker-compose" else []
            return _run(
                [docker, *subcommand, *args[1:]],
                cwd=cwd,
                env={**os.environ, "COMPOSE_PROJECT_NAME": "drycontroller-edge"},
            )
        raise HostReleaseError("unexpected real E2E command")

    try:
        network_id = _run(
            [
                docker,
                "network",
                "create",
                "--driver",
                "bridge",
                "--label",
                "drycontroller.purpose=host-release-e2e",
                network_name,
            ]
        ).strip()
        network_created = True
        assert re.fullmatch(r"[0-9a-f]{64}", network_id)
        _run(
            [
                docker,
                "run",
                "-d",
                "--name",
                registry_name,
                "--network",
                network_id,
                "-p",
                f"{registry_port}:5000",
                "registry:2",
            ]
        )
        for sequence, services in ((1, ("edge-api",)), (2, ("edge-api", "worker")), (3, ("edge-api", "worker"))):
            host_id = f"{sequence:08d}-1111-4111-8111-{sequence:012d}"
            software_id = f"{sequence:08d}-2222-4222-8222-{sequence:012d}"
            files = _files(services, f"{sequence}.0.0")
            service_refs: dict[str, str] = {}
            image_ids: dict[str, str] = {}
            for service in services:
                local = f"drycontroller-rb5c-{suffix}-{service}:{sequence}"
                target = f"{registry}/drycontroller/{service}:{sequence}"
                _run(
                    [docker, "build", "-q", "--build-arg", f"VERSION={sequence}", "-t", local, "-"],
                    input_text=(
                        "FROM busybox:latest\n"
                        "ARG VERSION\n"
                        f"LABEL drycontroller.rb5c.service=\"{service}\"\n"
                        "LABEL drycontroller.rb5c.version=\"$VERSION\"\n"
                        'CMD ["sh","-c","sleep 300"]\n'
                    ),
                )
                created_images.update({local, target})
                reference, image_id = _push_and_sign(
                    docker,
                    cosign,
                    local=local,
                    target=target,
                    key=software_key,
                    password=password,
                    signing_config=signing_config,
                )
                service_refs[service] = reference
                image_ids[service] = image_id
                created_images.update({reference, _local_image_reference(service, image_id)})
            compose = files["docker-compose.yml"][0]
            topology = {
                "compose_digest": "sha256:" + hashlib.sha256(compose).hexdigest(),
                "service_inventory": sorted(services),
                "health_contract": HOST_HEALTH_CONTRACT,
            }
            software = {
                "schema": "drycontroller.software-release.v1",
                "release_id": software_id,
                "host_release_id": host_id,
                "topology_digest": canonical_topology_digest(topology),
                "sequence": sequence,
                "version": f"{sequence}.0.0",
                "source_commit": f"{sequence:x}" * 40,
                "registry": registry,
                "signing_key_sha256": "sha256:" + hashlib.sha256(software_pub.read_bytes()).hexdigest(),
                "cosign": COSIGN_TOOLCHAIN,
                "services": service_refs,
                "image_ids": image_ids,
            }
            software_signature = _sign_blob(
                cosign,
                software,
                key=software_key,
                password=password,
                signing_config=signing_config,
                output=tmp_path / f"software-{sequence}.sigstore.json",
            )
            layer = tmp_path / f"host-{sequence}.tar"
            _bundle_tar(layer, files)
            host_local = f"drycontroller-rb5c-{suffix}-host:{sequence}"
            host_target = f"{registry}/drycontroller/host-bundle:{sequence}"
            _run([docker, "import", str(layer), host_local])
            created_images.update({host_local, host_target})
            bundle_reference, bundle_image_id = _push_and_sign(
                docker,
                cosign,
                local=host_local,
                target=host_target,
                key=host_key,
                password=password,
                signing_config=signing_config,
            )
            created_images.add(bundle_reference)
            host = _host_manifest(
                release_id=host_id,
                sequence=sequence,
                registry=registry,
                host_key=host_pub.read_bytes(),
                bundle_reference=bundle_reference,
                bundle_image_id=bundle_image_id,
                software=software,
                files=files,
            )
            host_signature = _sign_blob(
                cosign,
                host,
                key=host_key,
                password=password,
                signing_config=signing_config,
                output=tmp_path / f"host-{sequence}.sigstore.json",
            )
            releases.append(
                {
                    "host": host,
                    "host_signature": host_signature,
                    "software": software,
                    "software_signature": software_signature,
                    "files": files,
                }
            )

        first = releases[0]
        first_root = paths.releases / first["host"]["release_id"]
        for relative, (data, mode) in first["files"].items():
            destination = first_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(mode)
        (first_root / ".host-release-manifest.json").write_bytes(canonical_json_bytes(first["host"]))
        (first_root / ".host-release-signature.txt").write_text(first["host_signature"])
        first_software_digest = canonical_digest(first["software"])
        evidence_dir = paths.software_releases / first_software_digest.removeprefix("sha256:")
        evidence_dir.mkdir()
        evidence = {"manifest_digest": first_software_digest, "images": {}}
        for service, remote in first["software"]["services"].items():
            image_id = first["software"]["image_ids"][service]
            local = _local_image_reference(service, image_id)
            _run([docker, "tag", remote, local])
            created_images.add(local)
            runtime_id = _run([docker, "image", "inspect", "--format", "{{.Id}}", local]).strip()
            evidence["images"][service] = {
                "reference": local,
                "image_id": image_id,
                "runtime_id": runtime_id,
            }
        (evidence_dir / "local-images.json").write_bytes(canonical_json_bytes(evidence))
        active = {
            "host_manifest": first["host"],
            "host_manifest_digest": canonical_digest(first["host"]),
            "host_manifest_signature": first["host_signature"],
            "software_manifest": first["software"],
            "software_manifest_digest": first_software_digest,
            "software_manifest_signature": first["software_signature"],
            "host_path": str(first_root),
        }
        paths.current_link.symlink_to(first_root)
        paths.compatibility_link.symlink_to(paths.current_link)
        paths.host_active.write_bytes(
            canonical_json_bytes(
                {
                    "schema": HOST_ACTIVE_SCHEMA,
                    "host_release_id": first["host"]["release_id"],
                    "host_release_manifest_digest": canonical_digest(first["host"]),
                    "topology_digest": first["host"]["topology_digest"],
                    "compose_digest": first["host"]["topology"]["compose_digest"],
                    "service_inventory": first["host"]["topology"]["service_inventory"],
                }
            )
        )
        paths.software_journal.write_bytes(
            canonical_json_bytes(
                {
                    "schema": "drycontroller.release-journal.v1",
                    "phase": "idle",
                    "operation_id": None,
                    "last_operation_id": None,
                    "active": {
                        "manifest": first["software"],
                        "manifest_digest": first_software_digest,
                        "manifest_signature": first["software_signature"],
                        "verified_at": "2026-08-13T00:00:00Z",
                    },
                    "candidate": None,
                    "previous": None,
                    "effect": None,
                    "error": None,
                }
            )
        )
        paths.journal.write_bytes(
            canonical_json_bytes(
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
            )
        )

        engine = HostReleaseEngine(paths=paths, command_runner=command_runner)
        engine._start_slot(active, 999)

        def policy(operation_id: str, release: dict) -> dict:
            return {
                "state": "authorized",
                "operation_id": operation_id,
                "tenant_id": "tenant-a",
                "line_id": "line-a",
                "device_id": "EDGE-001",
                "node_name": "edge-001",
                "node_id": int(operation_id[16:], 16),
                "host_release_id": release["host"]["release_id"],
                "host_manifest_digest": canonical_digest(release["host"]),
                "host_manifest_signature": release["host_signature"],
                "host_manifest": release["host"],
                "software_release_id": release["software"]["release_id"],
                "software_manifest_digest": canonical_digest(release["software"]),
                "software_manifest_signature": release["software_signature"],
                "software_manifest": release["software"],
            }

        second_operation = "00000000000000020000000000000007"
        paths.request.write_text(json.dumps({"operation_id": second_operation}))
        engine._policy_fetcher = lambda value: policy(value, releases[1])
        assert engine.apply()["status"] == "succeeded"
        after_second = json.loads(paths.journal.read_text())
        assert after_second["active"]["host_manifest"]["topology"]["service_inventory"] == ["edge-api", "worker"]
        assert paths.current_link.resolve() == paths.releases / releases[1]["host"]["release_id"]

        third_operation = "00000000000000030000000000000007"
        paths.request.write_text(json.dumps({"operation_id": third_operation}))
        engine._policy_fetcher = lambda value: policy(value, releases[2])
        with pytest.raises(HostReleaseError, match="candidate health failed"):
            engine.apply()

        recovered = json.loads(paths.journal.read_text())
        assert recovered["phase"] == "idle"
        assert recovered["active"]["host_manifest"]["release_id"] == releases[1]["host"]["release_id"]
        assert recovered["active"]["software_manifest"]["release_id"] == releases[1]["software"]["release_id"]
        assert paths.current_link.resolve() == paths.releases / releases[1]["host"]["release_id"]
        assert registry_offline_at is not None
        assert not any(args[:2] == ["/usr/bin/docker", "pull"] for args in commands[registry_offline_at:])
    finally:
        for release in releases:
            compose = paths.releases / release["host"]["release_id"] / "docker-compose.yml"
            if compose.exists():
                subprocess.run(
                    [docker, "compose", "--project-name", "drycontroller-edge", "--file", str(compose), "down", "--volumes", "--remove-orphans"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        subprocess.run(
            [docker, "rm", "-f", "-v", registry_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if created_images:
            subprocess.run(
                [docker, "image", "rm", "-f", *sorted(created_images)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if network_created:
            cleanup_network = (
                network_id
                if network_id is not None and re.fullmatch(r"[0-9a-f]{64}", network_id)
                else network_name
            )
            removed = subprocess.run(
                [docker, "network", "rm", cleanup_network],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if removed.returncode != 0:
                pytest.fail(f"failed to remove isolated Docker network {network_name}")
