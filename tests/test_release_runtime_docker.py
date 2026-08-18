from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER_DIR = ROOT / "infra" / "ota-helper"
if str(HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(HELPER_DIR))

from release_engine import (  # noqa: E402
    RELEASE_JOURNAL_SCHEMA,
    REGISTRY_AUTH_SCHEMA,
    COSIGN_TOOLCHAIN,
    ReleaseEngine,
    ReleaseError,
    ReleasePaths,
    canonical_manifest_bytes,
    canonical_manifest_digest,
    canonical_topology_digest,
    docker_archive_config_digest,
    local_image_reference,
)


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


def _registry_document(registry: str) -> dict:
    return {
        "schema": REGISTRY_AUTH_SCHEMA,
        "auths": {registry: {"auth": base64.b64encode(b"reader:secret").decode("ascii")}},
        "credential": {
            "status": "active",
            "credential_id": "cred-1",
            "registry_host": registry,
            "username": "reader",
            "secret_fingerprint": hashlib.sha256(b"secret").hexdigest()[:16],
            "version": 2,
            "transaction_id": "a" * 32,
            "audit_id": 17,
            "updated_at": "2026-08-19T00:00:00+00:00",
        },
    }


def _docker_run(
    args: list[str],
    cwd: Path,
    timeout: float | None = None,
    *,
    runtime_env: Path | None = None,
) -> str:
    forwarded = [str(runtime_env) if runtime_env and arg == "/etc/drycontroller/runtime.env" else arg for arg in args[1:]]
    actual = [shutil.which("docker") or "docker", *forwarded]
    completed = subprocess.run(
        actual,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise ReleaseError(f"real Docker command failed: {actual[1:3]}")
    return completed.stdout


def _sign_manifest(cosign: str, manifest: dict, key: Path, signing_config: Path, directory: Path) -> str:
    manifest_path = directory / f"{manifest['version']}.json"
    bundle_path = directory / f"{manifest['version']}.sigstore.json"
    manifest_path.write_bytes(canonical_manifest_bytes(manifest))
    subprocess.run(
        [
            cosign,
            "sign-blob",
            "--yes",
            "--signing-config",
            str(signing_config),
            "--key",
            str(key),
            "--bundle",
            str(bundle_path),
            str(manifest_path),
        ],
        check=True,
        env={**os.environ, "COSIGN_PASSWORD": "rb4-docker-e2e"},
        stdout=subprocess.DEVNULL,
    )
    return base64.b64encode(bundle_path.read_bytes()).decode("ascii")


@pytest.mark.skipif(
    os.environ.get("DRYCONTROLLER_RUN_DOCKER_RELEASE_TEST") != "1",
    reason="set DRYCONTROLLER_RUN_DOCKER_RELEASE_TEST=1 for isolated real Docker readiness verification",
)
def test_release_runtime_waits_for_real_compose_health_start_period(tmp_path: Path):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker is unavailable")
    projects = json.loads(
        subprocess.check_output([docker, "compose", "ls", "--all", "--format", "json"], text=True)
    )
    if any(project.get("Name") == "drycontroller-edge" for project in projects):
        pytest.fail("isolated Compose project drycontroller-edge is already in use")

    image_archive = tmp_path / "busybox.image.tar"
    subprocess.run([docker, "image", "save", "-o", str(image_archive), "busybox:latest"], check=True)
    image_id = docker_archive_config_digest(image_archive)
    image_archive.unlink()
    compose = tmp_path / "docker-compose.yml"
    override = tmp_path / "release.override.yml"
    compose_lines = ["services:"]
    local_references = {
        service: local_image_reference(service, image_id) for service in SERVICES
    }
    for reference in local_references.values():
        subprocess.run([docker, "tag", "busybox:latest", reference], check=True)
    override_lines = ["services:"]
    for service in SERVICES:
        compose_lines.extend([f"  {service}:", "    image: busybox:latest"])
        if service == "mosquitto-init":
            compose_lines.append('    command: ["sh", "-c", "sleep 2"]')
        else:
            compose_lines.extend(
                [
                    '    command: ["sh", "-c", "sleep 2; touch /tmp/ready; sleep 300"]',
                    "    healthcheck:",
                    '      test: ["CMD-SHELL", "test -f /tmp/ready"]',
                    "      interval: 500ms",
                    "      timeout: 1s",
                    "      retries: 5",
                    "      start_period: 5s",
                ]
            )
        override_lines.extend([f"  {service}:", f"    image: {json.dumps(local_references[service])}"])
    compose.write_text("\n".join(compose_lines) + "\n", encoding="utf-8")
    override.write_text("\n".join(override_lines) + "\n", encoding="utf-8")

    manifest = {
        "schema": "drycontroller.software-release.v1",
        "release_id": "11111111-1111-4111-8111-111111111111",
        "host_release_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "topology_digest": "sha256:" + "c" * 64,
        "sequence": 1,
        "version": "1.0.0",
        "source_commit": "a" * 40,
        "registry": "registry.example.test:5443",
        "signing_key_sha256": "sha256:" + "b" * 64,
        "cosign": COSIGN_TOOLCHAIN,
        "services": {
            service: f"registry.example.test:5443/drycontroller/{service}@sha256:{index:064x}"
            for index, service in enumerate(SERVICES, start=1)
        },
        "image_ids": {service: image_id for service in SERVICES},
    }
    journal = {
        "schema": RELEASE_JOURNAL_SCHEMA,
        "phase": "candidate_apply_started",
        "operation_id": "0" * 32,
        "last_operation_id": None,
        "active": None,
        "candidate": None,
        "previous": None,
        "effect": None,
        "error": None,
    }
    paths = ReleasePaths(
        lock=tmp_path / "release.lock",
        journal=tmp_path / "release-journal.json",
        request=tmp_path / "request.json",
        releases=tmp_path / "releases",
        project=tmp_path,
        compose=compose,
        cosign_pubkey=tmp_path / "cosign.pub",
        registry_status=tmp_path / "registry.json",
        device=tmp_path / "device.json",
        cloud_config=tmp_path / "cloud.env",
        host_active=tmp_path / "host-active.json",
    )
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text("", encoding="utf-8")
    status_checks = 0

    def run(args: list[str], timeout: float | None = None) -> str:
        nonlocal status_checks
        actual = [
            docker,
            *(str(runtime_env) if arg == "/etc/drycontroller/runtime.env" else arg for arg in args[1:]),
        ]
        if "ps" in actual:
            status_checks += 1
        completed = subprocess.run(
            actual,
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise ReleaseError("real Docker readiness command failed")
        return completed.stdout

    engine = ReleaseEngine(
        paths=paths,
        command_runner=run,
        runtime_command_runner=lambda args, timeout: run(args, timeout),
        health_checker=lambda _url: None,
    )
    try:
        run(engine._compose_args(override, "up", "-d", "--pull", "never", "--no-build"))
        engine._verify_runtime(journal, {"manifest": manifest}, override, "candidate")
        assert status_checks >= 2
    finally:
        subprocess.run(
            [
                docker,
                "compose",
                "--project-name",
                "drycontroller-edge",
                "--file",
                str(compose),
                "--file",
                str(override),
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            cwd=tmp_path,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            [docker, "image", "rm", "-f", *local_references.values()],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


@pytest.mark.skipif(
    os.environ.get("DRYCONTROLLER_RUN_DOCKER_RELEASE_E2E") != "1",
    reason="set DRYCONTROLLER_RUN_DOCKER_RELEASE_E2E=1 for isolated signed Registry release E2E",
)
def test_signed_registry_a_b_c_failure_rolls_back_offline_and_rejects_wrong_topology_before_pull(
    tmp_path: Path,
):
    docker = shutil.which("docker")
    cosign = shutil.which("cosign")
    if docker is None or cosign is None:
        pytest.skip("docker and cosign are required")
    projects = json.loads(
        subprocess.check_output([docker, "compose", "ls", "--all", "--format", "json"], text=True)
    )
    if any(project.get("Name") == "drycontroller-edge" for project in projects):
        pytest.fail("isolated Compose project drycontroller-edge is already in use")

    suffix = f"{os.getpid()}-{secrets.token_hex(6)}"
    registry_name = f"drycontroller-rb4-registry-{suffix}"
    network_name = f"drycontroller-rb4-network-{suffix}"
    registry_port = 55000 + os.getpid() % 1000
    registry = f"127.0.0.1:{registry_port}"
    key_prefix = tmp_path / "release-cosign"
    signing_config = tmp_path / "signing-config.json"
    project = tmp_path / "project"
    project.mkdir()
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text("", encoding="utf-8")
    compose = project / "docker-compose.yml"
    compose.write_text(
        "services:\n"
        "  edge-api:\n"
        "    image: busybox:latest\n"
        '    command: ["sh", "-c", "sleep 300"]\n',
        encoding="utf-8",
    )
    host_release_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    compose_digest = "sha256:" + hashlib.sha256(compose.read_bytes()).hexdigest()
    topology_digest = canonical_topology_digest(compose_digest, ["edge-api"])
    host_active = tmp_path / "host-active.json"
    host_active.write_text(
        json.dumps(
            {
                "schema": "drycontroller.host-active.v1",
                "host_release_id": host_release_id,
                "host_release_manifest_digest": "sha256:" + "d" * 64,
                "topology_digest": topology_digest,
                "compose_digest": compose_digest,
                "service_inventory": ["edge-api"],
            }
        ),
        encoding="utf-8",
    )
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
        check=True,
        env={**os.environ, "COSIGN_PASSWORD": "rb4-docker-e2e"},
        stdout=subprocess.DEVNULL,
    )
    pubkey = key_prefix.with_suffix(".pub")
    key_digest = "sha256:" + hashlib.sha256(pubkey.read_bytes()).hexdigest()
    image_ids: dict[str, str] = {}
    references: dict[str, str] = {}
    manifests: list[dict] = []
    network_created = False
    network_id: str | None = None

    def command(args: list[str]) -> str:
        if args[0] == "/usr/local/bin/cosign":
            actual = [cosign, *args[1:]]
            if len(actual) > 1 and actual[1] == "verify":
                actual.insert(2, "--allow-insecure-registry")
            completed = subprocess.run(actual, cwd=project, check=False, capture_output=True, text=True)
            if completed.returncode != 0:
                raise ReleaseError("real cosign verification failed")
            return completed.stdout
        return _docker_run(args, project, runtime_env=runtime_env)

    try:
        network_id = subprocess.check_output(
            [
                docker,
                "network",
                "create",
                "--driver",
                "bridge",
                "--label",
                "drycontroller.purpose=software-release-e2e",
                network_name,
            ],
            text=True,
        ).strip()
        network_created = True
        assert re.fullmatch(r"[0-9a-f]{64}", network_id)
        subprocess.run(
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
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        for sequence, version in enumerate(("1.0.0", "1.1.0", "1.2.0", "1.3.0"), start=1):
            local = f"drycontroller-rb4-{suffix}:{version}"
            reference = f"{registry}/drycontroller/edge-api:{version}"
            subprocess.run(
                [docker, "build", "-q", "--build-arg", f"VERSION={version}", "-t", local, "-"],
                input='FROM busybox:latest\nARG VERSION\nLABEL drycontroller.release="$VERSION"\nCMD ["sh","-c","sleep 300"]\n',
                text=True,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            subprocess.run([docker, "tag", local, reference], check=True)
            subprocess.run([docker, "push", reference], check=True, stdout=subprocess.DEVNULL)
            digest_ref = subprocess.check_output(
                [docker, "image", "inspect", "--format", "{{index .RepoDigests 0}}", reference],
                text=True,
            ).strip()
            image_archive = tmp_path / f"{version}.image.tar"
            subprocess.run([docker, "image", "save", "-o", str(image_archive), reference], check=True)
            image_id = docker_archive_config_digest(image_archive)
            image_archive.unlink()
            references[version] = digest_ref
            image_ids[version] = image_id
            if sequence == 1:
                subprocess.run(
                    [docker, "tag", reference, local_image_reference("edge-api", image_id)],
                    check=True,
                )
            subprocess.run(
                [
                    cosign,
                    "sign",
                    "--yes",
                    "--allow-insecure-registry",
                    "--signing-config",
                    str(signing_config),
                    "--key",
                    str(key_prefix.with_suffix(".key")),
                    digest_ref,
                ],
                check=True,
                env={**os.environ, "COSIGN_PASSWORD": "rb4-docker-e2e"},
                stdout=subprocess.DEVNULL,
            )
            manifest = {
                "schema": "drycontroller.software-release.v1",
                "release_id": f"{sequence:08d}-0000-4000-8000-{sequence:012d}",
                "host_release_id": host_release_id,
                "topology_digest": topology_digest,
                "sequence": sequence,
                "version": version,
                "source_commit": f"{sequence:x}" * 40,
                "registry": registry,
                "signing_key_sha256": key_digest,
                "cosign": COSIGN_TOOLCHAIN,
                "services": {"edge-api": digest_ref},
                "image_ids": {"edge-api": image_id},
            }
            signature = _sign_manifest(cosign, manifest, key_prefix.with_suffix(".key"), signing_config, tmp_path)
            manifests.append({"manifest": manifest, "signature": signature})

        paths = ReleasePaths(
            lock=tmp_path / "release.lock",
            journal=tmp_path / "release-journal.json",
            request=tmp_path / "request.json",
            releases=tmp_path / "releases",
            project=project,
            compose=compose,
            cosign_pubkey=pubkey,
            registry_status=tmp_path / "registry.json",
            device=tmp_path / "device.json",
            cloud_config=tmp_path / "cloud.env",
            host_active=host_active,
        )
        paths.registry_status.write_text(
            json.dumps(_registry_document(registry)), encoding="utf-8"
        )
        paths.registry_status.chmod(0o400)
        paths.device.write_text(
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
        first = manifests[0]
        engine = ReleaseEngine(
            paths=paths,
            command_runner=command,
            runtime_command_runner=lambda args, timeout: _docker_run(
                args,
                project,
                timeout,
                runtime_env=runtime_env,
            ),
            runtime_health_checker=lambda _url, _timeout: None,
        )
        engine._image_set_verifier = engine._verify_portable_image_set
        engine.initialize_active(
            {
                "manifest": first["manifest"],
                "manifest_digest": canonical_manifest_digest(first["manifest"]),
                "manifest_signature": first["signature"],
            }
        )
        engine.start_active()

        for index in (1, 2):
            operation_id = f"{index:016x}{2:016x}"
            release = manifests[index]
            paths.request.write_text(json.dumps({"operation_id": operation_id}), encoding="utf-8")
            policy = {
                "operation_id": operation_id,
                "release_id": release["manifest"]["release_id"],
                "manifest_digest": canonical_manifest_digest(release["manifest"]),
                "manifest_signature": release["signature"],
                "manifest": release["manifest"],
                "tenant_id": "tenant-a",
                "line_id": "line-a",
                "device_id": "EDGE-001",
                "node_name": "edge-001",
                "node_id": 2,
            }
            engine._policy_fetcher = lambda _operation_id, policy=policy: policy
            assert engine.apply()["status"] == "succeeded"

        wrong = {
            **manifests[3]["manifest"],
            "topology_digest": "sha256:" + "e" * 64,
        }
        wrong_operation = f"{3:016x}{2:016x}"
        paths.request.write_text(json.dumps({"operation_id": wrong_operation}), encoding="utf-8")
        wrong_policy = {
            "operation_id": wrong_operation,
            "release_id": wrong["release_id"],
            "manifest_digest": canonical_manifest_digest(wrong),
            "manifest_signature": manifests[3]["signature"],
            "manifest": wrong,
            "tenant_id": "tenant-a",
            "line_id": "line-a",
            "device_id": "EDGE-001",
            "node_name": "edge-001",
            "node_id": 2,
        }
        engine._policy_fetcher = lambda _operation_id: wrong_policy
        with pytest.raises(ReleaseError, match="HostRelease binding mismatch"):
            engine.apply()

        failed = manifests[3]
        failure_operation = f"{4:016x}{2:016x}"
        paths.request.write_text(json.dumps({"operation_id": failure_operation}), encoding="utf-8")
        failure_policy = {
            "operation_id": failure_operation,
            "release_id": failed["manifest"]["release_id"],
            "manifest_digest": canonical_manifest_digest(failed["manifest"]),
            "manifest_signature": failed["signature"],
            "manifest": failed["manifest"],
            "tenant_id": "tenant-a",
            "line_id": "line-a",
            "device_id": "EDGE-001",
            "node_name": "edge-001",
            "node_id": 2,
        }
        engine._policy_fetcher = lambda _operation_id: failure_policy
        original_verify_runtime = engine._verify_runtime
        candidate_failed = False

        def fail_candidate(journal, slot, override, prefix):
            nonlocal candidate_failed
            if prefix == "candidate" and not candidate_failed:
                candidate_failed = True
                subprocess.run([docker, "stop", registry_name], check=True, stdout=subprocess.DEVNULL)
                raise ReleaseError("candidate health failed")
            return original_verify_runtime(journal, slot, override, prefix)

        engine._verify_runtime = fail_candidate  # type: ignore[method-assign]
        with pytest.raises(ReleaseError, match="candidate health failed"):
            engine.apply()
        journal = json.loads(paths.journal.read_text(encoding="utf-8"))
        assert journal["phase"] == "idle"
        assert journal["active"]["manifest"]["version"] == "1.2.0"
        running = json.loads(
            _docker_run(
                engine._compose_args(
                    paths.releases / journal["active"]["manifest_digest"].removeprefix("sha256:") / "compose-images.yml",
                    "ps",
                    "--format",
                    "json",
                ),
                project,
                runtime_env=runtime_env,
            )
        )
        if isinstance(running, dict):
            running = [running]
        assert running[0]["Image"] == local_image_reference("edge-api", image_ids["1.2.0"])
    finally:
        subprocess.run([docker, "rm", "-f", registry_name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(
            [docker, "compose", "--project-name", "drycontroller-edge", "--file", str(compose), "down", "--volumes"],
            cwd=project,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for version in ("1.0.0", "1.1.0", "1.2.0", "1.3.0"):
            for reference in (
                f"drycontroller-rb4-{suffix}:{version}",
                f"{registry}/drycontroller/edge-api:{version}",
                *(
                    [local_image_reference("edge-api", image_ids[version])]
                    if version in image_ids
                    else []
                ),
            ):
                subprocess.run([docker, "image", "rm", "-f", reference], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
