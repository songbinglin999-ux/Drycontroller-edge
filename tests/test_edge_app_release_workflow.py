from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "scripts/ci/edge_app_release_builder.py"
SPEC = importlib.util.spec_from_file_location("edge_app_release_builder", BUILDER_PATH)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)

BASELINE_PATH = ROOT / "scripts/create-edge-app-baseline.py"
BASELINE_SPEC = importlib.util.spec_from_file_location("create_edge_app_baseline", BASELINE_PATH)
assert BASELINE_SPEC is not None and BASELINE_SPEC.loader is not None
baseline = importlib.util.module_from_spec(BASELINE_SPEC)
BASELINE_SPEC.loader.exec_module(baseline)


def test_builder_emits_only_edge_app_v1_two_service_manifest(tmp_path: Path, monkeypatch):
    metadata = tmp_path / "release.json"
    images = tmp_path / "images.tsv"
    output = tmp_path / "manifest.json"
    metadata.write_text(
        json.dumps({"id": "11111111-1111-4111-8111-111111111111", "sequence": 1})
    )
    images.write_text(
        "edge-api\tregistry.test/edge-api@sha256:" + "1" * 64 + "\n"
        "edge-daq\tregistry.test/edge-daq@sha256:" + "2" * 64 + "\n"
    )
    monkeypatch.setenv("RELEASE_SOURCE_COMMIT", "3" * 40)

    builder.manifest(metadata, images, output)

    manifest = json.loads(output.read_text())
    assert set(manifest) == {
        "schema",
        "profile",
        "release_id",
        "sequence",
        "source_commit",
        "deployment_digest",
        "images",
    }
    assert manifest["profile"] == "edge-app-v1"
    assert set(manifest["images"]) == {"edge-api", "edge-daq"}


def test_active_workflow_uses_new_builder_and_legacy_workflow_is_disabled():
    active = (ROOT / ".gitea/workflows/edge-app-release.yml").read_text()
    legacy = (ROOT / ".gitea/workflows/edge-software-release.yml").read_text()
    script = (ROOT / "scripts/ci/build-edge-app-release.sh").read_text()

    assert "build-edge-app-release.sh" in active
    assert "edge-app-v1" in active
    assert legacy.count("if: ${{ false }}") == 2
    assert "license-service" not in script
    assert "HOST_RELEASE" not in script


def test_baseline_snapshot_is_read_only_and_requires_approved_live_ids(tmp_path: Path):
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n")
    public_key = tmp_path / "cosign.pub"
    public_key.write_text("test")
    output = tmp_path / "manifest.json"
    ids = {
        "edge-api": "sha256:" + "1" * 64,
        "edge-daq": "sha256:" + "2" * 64,
    }
    calls: list[list[str]] = []

    def run(args: list[str]) -> str:
        calls.append(args)
        if args[:2] == ["/usr/bin/docker", "inspect"]:
            return json.dumps(
                [{"Image": ids[args[2]], "State": {"Health": {"Status": "healthy"}}}]
            )
        if args[:3] == ["/usr/bin/docker", "image", "inspect"]:
            service = "edge-api" if args[3] == ids["edge-api"] else "edge-daq"
            return json.dumps(
                [{"RepoDigests": [f"registry.test/{service}@{ids[service]}"]}]
            )
        return ""

    manifest = baseline.snapshot(
        output=output,
        release_id="11111111-1111-4111-8111-111111111111",
        sequence=1,
        source_commit="3" * 40,
        expected_ids=ids,
        command_runner=run,
        compose=compose,
        public_key=public_key,
    )

    assert set(manifest["images"]) == {"edge-api", "edge-daq"}
    assert all("pull" not in call and "up" not in call for call in calls)
