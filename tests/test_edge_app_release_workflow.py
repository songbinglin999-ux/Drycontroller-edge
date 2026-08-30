from __future__ import annotations

import importlib.util
import json
import os
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
    legacy_host = (ROOT / ".gitea/workflows/edge-host-release.yml").read_text()
    legacy_recovery = (ROOT / ".gitea/workflows/edge-release-handoff-recovery.yml").read_text()
    script = (ROOT / "scripts/ci/build-edge-app-release.sh").read_text()

    assert "build-edge-app-release.sh" in active
    assert "edge-app-v1" in active
    assert legacy.count("if: ${{ false }}") == 2
    assert legacy_host.count("if: ${{ false }}") == 2
    assert legacy_recovery.count("if: ${{ false }}") == 1
    assert "license-service" not in script
    assert "HOST_RELEASE" not in script
    assert 'readonly DOCKER_BINARY="/usr/bin/docker"' in script
    assert 'readonly COSIGN_BINARY="/usr/local/bin/cosign"' in script
    assert 'readonly TRIVY_BINARY="/usr/local/bin/trivy"' in script
    assert "verify-edge-app-toolchain.py" in script
    assert "cosign public key fingerprint is not approved" in script
    assert "EDGE_APP_COSIGN_PUBLIC_KEY_SHA256" in active


def test_non_destructive_runtime_retrofit_retires_legacy_runner_without_touching_app_data():
    retrofit = (ROOT / "infra/bootstrap/retrofit-edge-app-runtime.sh").read_text()
    runtime_files = _load_runtime_files()

    assert "systemctl disable --now" in retrofit
    assert "ln -s /dev/null" in retrofit
    assert "legacy HostRelease socket remained" in retrofit
    assert "software cosign public key checksum is not approved" in retrofit
    assert "device identity Cloud public key checksum is not approved" in retrofit
    assert "systemctl enable --now drycontroller-tpm-identity-observer.service" in retrofit
    assert "systemctl enable drycontroller-ota-helper.service" in retrofit
    assert "docker pull" not in retrofit
    assert "docker compose" not in retrofit
    assert "initialize-active-release" not in retrofit
    assert not any("host-release" in source for source in runtime_files)


def _load_runtime_files() -> dict[str, tuple[str, int]]:
    path = ROOT / "infra/bootstrap/install-static-runtime.py"
    spec = importlib.util.spec_from_file_location("active_static_runtime", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RUNTIME_FILES


def test_edge_app_toolchain_verifier_binds_fixed_files_versions_and_platform(
    tmp_path: Path, monkeypatch
):
    path = ROOT / "scripts/ci/verify-edge-app-toolchain.py"
    spec = importlib.util.spec_from_file_location("edge_app_toolchain", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, version in (("docker", "24.0.2"), ("cosign", "v3.1.3"), ("trivy", "0.70.0")):
        binary = tmp_path / name
        binary.write_bytes(name.encode())
        binary.chmod(0o755)
        module.TOOLS[name] = {
            "path": binary,
            "sha256": __import__("hashlib").sha256(binary.read_bytes()).hexdigest(),
            "version": version,
        }
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(module, "_trusted_ancestors", lambda *_args: None)
    outputs = iter(("24.0.2|linux/amd64", "GitVersion: v3.1.3\nPlatform: linux/amd64", "Version: 0.70.0"))
    monkeypatch.setattr(module, "_output", lambda _command: next(outputs))

    receipt = module.verify_release_toolchain(owner_uid=os.getuid(), owner_gid=os.getgid())

    assert receipt["platform"] == "linux/amd64"
    assert set(receipt["tools"]) == {"docker", "cosign", "trivy"}


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
