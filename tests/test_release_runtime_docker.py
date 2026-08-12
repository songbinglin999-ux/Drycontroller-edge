from __future__ import annotations

import json
import os
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
    SOFTWARE_RELEASE_SERVICES,
    COSIGN_TOOLCHAIN,
    ReleaseEngine,
    ReleaseError,
    ReleasePaths,
)


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

    image_id = subprocess.check_output(
        [docker, "image", "inspect", "--format", "{{.Id}}", "busybox:latest"],
        text=True,
    ).strip()
    compose = tmp_path / "docker-compose.yml"
    override = tmp_path / "release.override.yml"
    compose_lines = ["services:"]
    override_lines = ["services:"]
    for service in SOFTWARE_RELEASE_SERVICES:
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
        override_lines.extend([f"  {service}:", f"    image: {json.dumps(image_id)}"])
    compose.write_text("\n".join(compose_lines) + "\n", encoding="utf-8")
    override.write_text("\n".join(override_lines) + "\n", encoding="utf-8")

    manifest = {
        "schema": "drycontroller.software-release.v1",
        "release_id": "11111111-1111-4111-8111-111111111111",
        "sequence": 1,
        "version": "1.0.0",
        "source_commit": "a" * 40,
        "registry": "registry.example.test:5443",
        "signing_key_sha256": "sha256:" + "b" * 64,
        "cosign": COSIGN_TOOLCHAIN,
        "services": {
            service: f"registry.example.test:5443/drycontroller/{service}@sha256:{index:064x}"
            for index, service in enumerate(SOFTWARE_RELEASE_SERVICES, start=1)
        },
        "image_ids": {service: image_id for service in SOFTWARE_RELEASE_SERVICES},
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
    )
    status_checks = 0

    def run(args: list[str], timeout: float | None = None) -> str:
        nonlocal status_checks
        actual = [docker, *args[1:]]
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
