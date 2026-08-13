from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER_DIR = ROOT / "infra" / "ota-helper"
if str(HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(HELPER_DIR))

from release_engine import (  # noqa: E402
    COSIGN_TOOLCHAIN,
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


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, text=True, **kwargs)


def _test_compose() -> str:
    lines = ["services:"]
    for service in SERVICES:
        lines.extend([f"  {service}:", "    image: busybox:latest"])
        if service == "mosquitto-init":
            lines.append('    command: ["sh", "-c", "exit 0"]')
        elif service == "edge-api":
            lines.extend(
                [
                    '    command: ["sh", "-c", "mkdir -p /www; echo ok > /www/ready; httpd -f -p 8000 -h /www"]',
                    '    ports: ["8000:8000"]',
                ]
            )
        elif service == "ai-softsensor":
            lines.extend(
                [
                    '    command: ["sh", "-c", "mkdir -p /www; echo ok > /www/ready; httpd -f -p 8082 -h /www"]',
                    '    ports: ["8082:8082"]',
                ]
            )
        else:
            lines.append('    command: ["sh", "-c", "sleep 300"]')
    return "\n".join(lines) + "\n"


@pytest.mark.skipif(
    os.environ.get("DRYCONTROLLER_RUN_INSTALLER_DOCKER_E2E") != "1",
    reason="set DRYCONTROLLER_RUN_INSTALLER_DOCKER_E2E=1 for isolated root/systemd/Docker install E2E",
)
def test_root_systemd_installer_serializes_two_blank_installs_and_starts_signed_h1_t1_s1(
    tmp_path: Path,
):
    docker = shutil.which("docker")
    cosign = shutil.which("cosign")
    linux_cosign_value = os.environ.get("COSIGN_LINUX_AMD64", "")
    linux_cosign = Path(linux_cosign_value)
    if docker is None or cosign is None or not linux_cosign.is_file():
        pytest.skip("docker, host cosign, and COSIGN_LINUX_AMD64 are required")
    docker_arch = subprocess.check_output(
        [docker, "info", "--format", "{{.Architecture}}"], text=True
    ).strip()
    if docker_arch not in {"amd64", "x86_64"}:
        pytest.skip("native amd64 Docker is required for the fixed production cosign binary")

    suffix = f"{os.getpid()}-{int(time.time())}"
    systemd_image = f"drycontroller-rb4-systemd-e2e:{suffix}"
    release_image = f"drycontroller-rb4-installer-release:{suffix}"
    container = f"drycontroller-rb4-installer-{suffix}"
    source = tmp_path / "source"
    media = tmp_path / "media"
    media.mkdir()
    shutil.copytree(
        ROOT,
        source,
        ignore=shutil.ignore_patterns(".git", ".codegraph", ".pytest_cache", "__pycache__", "dist"),
    )
    compose = source / "docker-compose.yml"
    compose.write_text(_test_compose(), encoding="utf-8")
    dockerfile = tmp_path / "Dockerfile.systemd"
    dockerfile.write_text(
        "FROM ubuntu:24.04\n"
        "ENV DEBIAN_FRONTEND=noninteractive\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends "
        "systemd systemd-sysv docker.io docker-compose-v2 curl python3 ca-certificates && "
        "rm -rf /var/lib/apt/lists/* && "
        "printf '#!/bin/sh\\necho 1.0-test\\n' > /usr/local/bin/tailscale && "
        "chmod 0755 /usr/local/bin/tailscale\n"
        "RUN mkdir -p /etc/docker && printf "
        "'{\"storage-driver\":\"vfs\",\"iptables\":false,\"ip6tables\":false}\\n' "
        "> /etc/docker/daemon.json\n"
        "RUN update-alternatives --set iptables /usr/sbin/iptables-legacy && "
        "update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy\n"
        "ENV container=docker\n"
        "STOPSIGNAL SIGRTMIN+3\n"
        'CMD ["/sbin/init"]\n',
        encoding="utf-8",
    )
    release_dockerfile = tmp_path / "Dockerfile.release"
    release_dockerfile.write_text(
        "FROM busybox:latest\nLABEL drycontroller.release=installer-e2e\nCMD [\"sh\",\"-c\",\"sleep 300\"]\n",
        encoding="utf-8",
    )
    key_prefix = tmp_path / "release-cosign"
    signing_config = tmp_path / "signing-config.json"
    local_references: list[str] = []

    try:
        _run([docker, "build", "--platform", "linux/amd64", "-t", systemd_image, "-f", str(dockerfile), str(tmp_path)])
        _run(
            [docker, "build", "--platform", "linux/amd64", "-t", release_image, "-f", str(release_dockerfile), str(tmp_path)],
            stdout=subprocess.DEVNULL,
        )
        probe_bundle = media / "probe-images.tar"
        _run([docker, "image", "save", "-o", str(probe_bundle), release_image])
        image_id = docker_archive_config_digest(probe_bundle)
        probe_bundle.unlink()
        local_references = [local_image_reference(service, image_id) for service in SERVICES]
        for reference in local_references:
            _run([docker, "tag", release_image, reference])
        _run([docker, "image", "save", "-o", str(media / "images.tar"), *local_references])

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
        signing_env = {**os.environ, "COSIGN_PASSWORD": "rb4-installer-e2e"}
        _run(
            [cosign, "generate-key-pair", "--output-key-prefix", str(key_prefix)],
            env=signing_env,
            stdout=subprocess.DEVNULL,
        )
        public_key = key_prefix.with_suffix(".pub")
        compose_digest = "sha256:" + hashlib.sha256(compose.read_bytes()).hexdigest()
        topology_digest = canonical_topology_digest(compose_digest, sorted(SERVICES))
        manifest = {
            "schema": "drycontroller.software-release.v1",
            "release_id": "11111111-1111-4111-8111-111111111111",
            "host_release_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "topology_digest": topology_digest,
            "sequence": 1,
            "version": "1.0.0",
            "source_commit": "a" * 40,
            "registry": "registry.example.test:5443",
            "signing_key_sha256": "sha256:" + hashlib.sha256(public_key.read_bytes()).hexdigest(),
            "cosign": COSIGN_TOOLCHAIN,
            "services": {
                service: f"registry.example.test:5443/drycontroller/{service}@sha256:{index:064x}"
                for index, service in enumerate(SERVICES, start=1)
            },
            "image_ids": {service: image_id for service in SERVICES},
        }
        manifest_path = media / "manifest.json"
        bundle_path = media / "manifest.sigstore.json"
        manifest_path.write_bytes(canonical_manifest_bytes(manifest))
        _run(
            [
                cosign,
                "sign-blob",
                "--yes",
                "--signing-config",
                str(signing_config),
                "--key",
                str(key_prefix.with_suffix(".key")),
                "--bundle",
                str(bundle_path),
                str(manifest_path),
            ],
            env=signing_env,
            stdout=subprocess.DEVNULL,
        )
        (media / "seed.json").write_text(
            json.dumps(
                {
                    "manifest": manifest,
                    "manifest_digest": canonical_manifest_digest(manifest),
                    "manifest_signature": base64.b64encode(bundle_path.read_bytes()).decode("ascii"),
                }
            ),
            encoding="utf-8",
        )
        (media / "host-active.json").write_text(
            json.dumps(
                {
                    "schema": "drycontroller.host-active.v1",
                    "host_release_id": manifest["host_release_id"],
                    "host_release_manifest_digest": "sha256:" + "d" * 64,
                    "topology_digest": topology_digest,
                    "compose_digest": compose_digest,
                    "service_inventory": sorted(SERVICES),
                }
            ),
            encoding="utf-8",
        )
        shutil.copy2(public_key, media / "cosign.pub")
        shutil.copy2(linux_cosign, media / "cosign-linux-amd64")

        _run(
            [
                docker,
                "run",
                "-d",
                "--privileged",
                "--platform",
                "linux/amd64",
                "--cgroupns",
                "host",
                "--name",
                container,
                "--tmpfs",
                "/run",
                "--tmpfs",
                "/run/lock",
                "-v",
                "/sys/fs/cgroup:/sys/fs/cgroup:rw",
                systemd_image,
            ],
            stdout=subprocess.DEVNULL,
        )
        _run([docker, "cp", f"{source}/.", f"{container}:/source"])
        _run([docker, "cp", f"{media}/.", f"{container}:/media"])
        signature_check = subprocess.run(
            [
                docker,
                "exec",
                container,
                "/media/cosign-linux-amd64",
                "verify-blob",
                "--key",
                "/media/cosign.pub",
                "--insecure-ignore-tlog=true",
                "--bundle",
                "/media/manifest.sigstore.json",
                "/media/manifest.json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert signature_check.returncode == 0, (
            f"linux cosign preflight failed: stdout={signature_check.stdout!r} "
            f"stderr={signature_check.stderr!r}"
        )
        for _ in range(60):
            state = subprocess.run(
                [docker, "exec", container, "systemctl", "is-system-running"],
                check=False,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if state in {"running", "degraded"}:
                break
            time.sleep(1)
        else:
            diagnostics = subprocess.run(
                [docker, "logs", container], check=False, capture_output=True, text=True
            )
            pytest.fail(
                "isolated systemd host did not become ready: "
                f"state={state!r} stdout={diagnostics.stdout[-4000:]!r} stderr={diagnostics.stderr[-4000:]!r}"
            )

        install = [
            docker,
            "exec",
            container,
            "bash",
            "/source/infra/bootstrap/install-edge-host.sh",
            "--source",
            "/source",
            "--skip-packages",
            "--initial-release",
            "/media/seed.json",
            "--host-active",
            "/media/host-active.json",
            "--image-bundle",
            "/media/images.tar",
            "--cosign-public-key",
            "/media/cosign.pub",
            "--cosign-bin",
            "/media/cosign-linux-amd64",
        ]
        installers = [subprocess.Popen(install, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for _ in range(2)]
        results = []
        for process in installers:
            output, _ = process.communicate(timeout=300)
            results.append((process.returncode, output))
        if sorted(result[0] for result in results) != [0, 1]:
            docker_journal = subprocess.run(
                [docker, "exec", container, "journalctl", "-u", "docker.service", "--no-pager", "-n", "120"],
                check=False,
                capture_output=True,
                text=True,
            )
            pytest.fail(f"installer results={results!r} docker_journal={docker_journal.stdout[-12000:]!r}")

        _run([docker, "exec", container, "systemctl", "is-active", "drycontroller-ota-helper.service"])
        _run([docker, "exec", container, "test", "-f", "/var/lib/drycontroller-ota/release-journal.json"])
        _run([docker, "exec", container, "test", "-f", "/var/lib/drycontroller-host/active.json"])
        mode = subprocess.check_output(
            [docker, "exec", container, "stat", "-c", "%U:%G:%a", "/var/lib/drycontroller-host/active.json"],
            text=True,
        ).strip()
        assert mode == "root:root:600"
        _run([docker, "exec", container, "/usr/libexec/drycontroller/start-active-release"])
        services = subprocess.check_output(
            [
                docker,
                "exec",
                container,
                "docker",
                "compose",
                "--project-name",
                "drycontroller-edge",
                "--file",
                "/opt/Drycontroller-edge/docker-compose.yml",
                "ps",
                "--services",
                "--status",
                "running",
            ],
            text=True,
        ).splitlines()
        assert set(services) == set(SERVICES) - {"mosquitto-init"}
    finally:
        subprocess.run([docker, "rm", "-f", container], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(
            [docker, "image", "rm", "-f", release_image, systemd_image, *local_references],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
