from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER_DIR = ROOT / "infra/ota-helper"
ALERTMANAGER_IMAGE = (
    "prom/alertmanager:v0.33.1@"
    "sha256:a89f8d4520954079275441eecdb71444328bd90633dd4eddfc33b9ed657f349b"
)


def test_alertmanager_image_is_pinned_consistently_for_runtime_and_release():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    release_builder = (ROOT / "scripts/ci/build-software-release.sh").read_text(encoding="utf-8")

    assert f"image: {ALERTMANAGER_IMAGE}" in compose
    assert f'alertmanager) printf \'%s\\n\' "{ALERTMANAGER_IMAGE}" ;;' in release_builder
    assert "prom/alertmanager:v0.27.0" not in compose
    assert "prom/alertmanager:v0.27.0" not in release_builder


def test_release_builder_is_complete_and_completion_is_last():
    script = (ROOT / "scripts/ci/build-software-release.sh").read_text(encoding="utf-8")
    expected = {
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
    }
    service_block = re.search(r"^SERVICES=\((.*?)^\)", script, flags=re.MULTILINE | re.DOTALL)
    assert service_block is not None
    assert set(re.findall(r'"([a-z0-9-]+)"', service_block.group(1))) == expected
    assert "release_step_resolve_predecessor" in script
    assert "release_step_resolve_changed_services" in script
    assert "release_step_load_inherited_images" in script
    assert "release_step_scan \"${inherited_ref}\"" in script
    assert "release_step_verify_image \"${inherited_ref}\"" in script
    assert "inputs.services" not in script
    assert '"host_release_id": os.environ["HOST_RELEASE_ID"]' in script
    assert '"topology_digest": os.environ["TOPOLOGY_DIGEST"]' in script
    assert "bash scripts/ci/verify-cosign-toolchain.sh" in script
    assert "release_step_resolve_portable_image_id" in script
    assert "docker_archive_config_digest" in script
    assert "docker image inspect --format '{{.Id}}'" not in script

    order = [
        "release_step_build_or_mirror",
        "release_step_scan",
        "release_step_push",
        "release_step_resolve_digest",
        "release_step_sign_image",
        "release_step_verify_image",
        "release_step_sign_manifest",
        "release_step_verify_manifest",
        "release_step_complete_cloud",
    ]
    positions = [script.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_release_cosign_toolchain_gate_checks_platform_version_and_checksum(tmp_path: Path):
    verifier = ROOT / "scripts" / "ci" / "verify-cosign-toolchain.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "uname").write_text(
        '#!/bin/sh\n[ "$1" = "-s" ] && echo "${FAKE_UNAME_S}" || echo "${FAKE_UNAME_M}"\n',
        encoding="utf-8",
    )
    (fake_bin / "cosign").write_text(
        '#!/bin/sh\nprintf "GitVersion: %s\\nPlatform: %s\\n" "${FAKE_COSIGN_VERSION}" "${FAKE_COSIGN_PLATFORM}"\n',
        encoding="utf-8",
    )
    (fake_bin / "sha256sum").write_text(
        '#!/bin/sh\nprintf "%s  %s\\n" "${FAKE_COSIGN_SHA256}" "$1"\n',
        encoding="utf-8",
    )
    for path in fake_bin.iterdir():
        path.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_UNAME_S": "Linux",
        "FAKE_UNAME_M": "x86_64",
        "FAKE_COSIGN_VERSION": "v3.1.3",
        "FAKE_COSIGN_PLATFORM": "linux/amd64",
        "FAKE_COSIGN_SHA256": "4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71",
    }

    accepted = subprocess.run(["bash", str(verifier)], env=environment, check=False)
    assert accepted.returncode == 0

    rejected_cases = (
        ({"FAKE_COSIGN_SHA256": "0" * 64}, "checksum is not approved"),
        ({"FAKE_COSIGN_VERSION": "v3.1.2"}, "version is not approved"),
        ({"FAKE_COSIGN_PLATFORM": "linux/arm64"}, "platform is not approved"),
        ({"FAKE_UNAME_S": "Darwin"}, "builder must be Linux"),
        ({"FAKE_UNAME_M": "aarch64"}, "builder must be amd64"),
    )
    for overrides, expected_error in rejected_cases:
        rejected = subprocess.run(
            ["bash", str(verifier)],
            env={**environment, **overrides},
            check=False,
            capture_output=True,
            text=True,
        )
        assert rejected.returncode == 1
        assert expected_error in rejected.stderr


def test_release_workflow_uses_the_fixed_builder_after_tests():
    workflow = (ROOT / ".gitea/workflows/edge-software-release.yml").read_text(encoding="utf-8")
    assert "needs: test" in workflow
    assert "bash scripts/ci/build-software-release.sh" in workflow
    assert "HOST_RELEASE_ID: ${{ vars.HOST_RELEASE_ID }}" in workflow
    assert "TOPOLOGY_DIGEST: ${{ vars.TOPOLOGY_DIGEST }}" in workflow
    assert "services:" not in workflow.split("workflow_dispatch:", 1)[1].split("jobs:", 1)[0]


def test_edge_test_workflows_install_bsdtar_before_the_full_suite():
    for relative in (
        ".gitea/workflows/edge-tests.yml",
        ".gitea/workflows/edge-host-release.yml",
        ".gitea/workflows/edge-software-release.yml",
    ):
        workflow = (ROOT / relative).read_text(encoding="utf-8")
        before_tests = workflow.split("bash scripts/test-all.sh", 1)[0]
        assert "apt-get install -y --no-install-recommends libarchive-tools" in before_tests
        assert "command -v bsdtar" in before_tests


def test_release_workflow_pins_actions_and_scopes_release_secrets_to_the_builder_step():
    workflow = (ROOT / ".gitea/workflows/edge-software-release.yml").read_text(encoding="utf-8")
    release_job = workflow.split("\n  release:\n", 1)[1]
    before_builder = release_job.split("      - name: Build and complete signed SoftwareRelease", 1)[0]
    builder = release_job.split("      - name: Build and complete signed SoftwareRelease", 1)[1]

    assert "actions/checkout@v4" not in workflow
    assert "actions/setup-python@v5" not in workflow
    assert "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683" in workflow
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065" in workflow
    for secret in (
        "HARBOR_PASSWORD",
        "COSIGN_PRIVATE_KEY",
        "COSIGN_PASSWORD",
        "CLOUD_OPS_RELEASE_API_KEY",
    ):
        assert secret not in before_builder
        assert f"{secret}: ${{{{ secrets.{secret} }}}}" in builder


def test_host_release_workflow_is_manual_serial_and_runs_the_fixed_builder_after_tests():
    workflow = (ROOT / ".gitea/workflows/edge-host-release.yml").read_text(encoding="utf-8")
    trigger = workflow.split("\nconcurrency:\n", 1)[0]

    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger
    assert "group: edge-release-transaction" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "needs: test" in workflow
    assert "bash scripts/ci/build-host-release.sh" in workflow
    assert "actions/checkout@v4" not in workflow
    assert "actions/setup-python@v5" not in workflow
    assert "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683" in workflow
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065" in workflow
    resolve_step = workflow.split("      - name: Resolve release identity", 1)[1].split(
        "      - name: Prime pinned Trivy database", 1
    )[0]
    run_script = resolve_step.split("        run: |", 1)[1]
    assert "INPUT_HOST_VERSION: ${{ inputs.host_version }}" in resolve_step
    assert "INPUT_SOFTWARE_VERSION: ${{ inputs.software_version }}" in resolve_step
    assert "${{ inputs." not in run_script
    assert '[[ "${GITHUB_REF}" == "refs/heads/master" ]]' in run_script


def test_host_release_workflow_scopes_both_signing_keys_to_the_builder_step():
    workflow = (ROOT / ".gitea/workflows/edge-host-release.yml").read_text(encoding="utf-8")
    release_job = workflow.split("\n  release:\n", 1)[1]
    before_builder, builder = release_job.split("      - name: Build and complete paired HostRelease", 1)

    for secret in (
        "HARBOR_PASSWORD",
        "HOST_COSIGN_PRIVATE_KEY",
        "HOST_COSIGN_PASSWORD",
        "SOFTWARE_COSIGN_PRIVATE_KEY",
        "SOFTWARE_COSIGN_PASSWORD",
        "CLOUD_OPS_RELEASE_API_KEY",
    ):
        assert secret not in before_builder
        expected = secret.removeprefix("SOFTWARE_") if secret.startswith("SOFTWARE_COSIGN") else secret
        assert f"{secret}: ${{{{ secrets.{expected} }}}}" in builder


def test_release_builder_only_rebuilds_directly_changed_services(tmp_path: Path):
    script = (ROOT / "scripts/ci/build-software-release.sh").read_text(encoding="utf-8")
    match = re.search(
        r"release_step_resolve_changed_services\(\) \{.*?<<'PY'\n(.*?)\nPY\n\}",
        script,
        flags=re.DOTALL,
    )
    assert match is not None
    resolver = match.group(1)
    services = (
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

    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "release-test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Release Test"], cwd=repository, check=True)
    (repository / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
    initial = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()

    service_dir = repository / "services" / "edge-api"
    service_dir.mkdir(parents=True)
    (service_dir / "app.py").write_text("changed = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "services/edge-api/app.py"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "change service"], cwd=repository, check=True)
    service_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()

    predecessor_path = tmp_path / "predecessor.json"
    output_path = tmp_path / "changed.txt"

    def resolve(previous_commit: str, current_commit: str) -> list[str]:
        import sys

        sys.path.insert(0, str(HELPER_DIR))
        from release_engine import COSIGN_TOOLCHAIN

        manifest = {
            "schema": "drycontroller.software-release.v1",
            "release_id": "11111111-1111-4111-8111-111111111111",
            "host_release_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "topology_digest": "sha256:" + "c" * 64,
            "sequence": 1,
            "version": "1.0.0",
            "source_commit": previous_commit,
            "registry": "registry.example.com",
            "signing_key_sha256": "sha256:" + "a" * 64,
            "cosign": COSIGN_TOOLCHAIN,
            "services": {
                service: f"registry.example.com/drycontroller/{service}@sha256:{index:064x}"
                for index, service in enumerate(services, start=1)
            },
            "image_ids": {
                service: f"sha256:{index:064x}"
                for index, service in enumerate(services, start=1)
            },
        }
        predecessor_path.write_text(
            json.dumps(
                {
                    "id": manifest["release_id"],
                    "sequence": 1,
                    "version": "1.0.0",
                    "source_commit": previous_commit,
                    "status": "completed",
                    "manifest": manifest,
                    "manifest_digest": "sha256:" + "b" * 64,
                    "manifest_signature": "c2ln",
                }
            ),
            encoding="utf-8",
        )
        environment = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "infra/ota-helper"),
            "HARBOR_HOST": "registry.example.com",
            "SIGNING_KEY_SHA256": "sha256:" + "a" * 64,
            "RELEASE_SOURCE_COMMIT": current_commit,
            "HOST_RELEASE_ID": manifest["host_release_id"],
            "TOPOLOGY_DIGEST": manifest["topology_digest"],
            "RELEASE_SERVICE_INVENTORY": "\n".join(services),
        }
        subprocess.run(
            ["python3", "-c", resolver, str(predecessor_path), str(output_path)],
            cwd=repository,
            env=environment,
            check=True,
        )
        return output_path.read_text(encoding="utf-8").splitlines()

    assert resolve(initial, service_commit) == ["edge-api"]

    docs_dir = repository / "docs"
    docs_dir.mkdir()
    (docs_dir / "release.md").write_text("docs only\n", encoding="utf-8")
    subprocess.run(["git", "add", "docs/release.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "docs only"], cwd=repository, check=True)
    docs_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    assert resolve(service_commit, docs_commit) == []

    common_dir = repository / "common"
    common_dir.mkdir()
    (common_dir / "shared.py").write_text("shared = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "common/shared.py"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "shared change"], cwd=repository, check=True)
    shared_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    assert set(resolve(docs_commit, shared_commit)) == set(services)
