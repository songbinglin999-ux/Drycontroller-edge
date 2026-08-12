from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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


def test_release_workflow_uses_the_fixed_builder_after_tests():
    workflow = (ROOT / ".gitea/workflows/edge-software-release.yml").read_text(encoding="utf-8")
    assert "needs: test" in workflow
    assert "bash scripts/ci/build-software-release.sh" in workflow
    assert "services:" not in workflow.split("workflow_dispatch:", 1)[1].split("jobs:", 1)[0]


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
        manifest = {
            "schema": "drycontroller.software-release.v1",
            "release_id": "11111111-1111-4111-8111-111111111111",
            "sequence": 1,
            "version": "1.0.0",
            "source_commit": previous_commit,
            "registry": "registry.example.com",
            "signing_key_sha256": "sha256:" + "a" * 64,
            "services": {
                service: f"registry.example.com/drycontroller/{service}@sha256:{index:064x}"
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
