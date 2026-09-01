from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Callable

import pytest


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


def test_active_workflow_and_executor_do_not_reach_legacy_release_paths():
    active = (ROOT / ".gitea/workflows/edge-app-release.yml").read_text()
    script = (ROOT / "scripts/ci/build-edge-app-release.sh").read_text()
    helper = (ROOT / "infra/ota-helper/edge_app_ota_helper.py").read_text()
    rollback = (ROOT / "infra/ota-helper/rollback-edge-app").read_text()
    docker_e2e = (ROOT / "tests/test_edge_app_release_runtime_docker.py").read_text()
    legacy_helper = (ROOT / "infra/ota-helper/drycontroller_ota_helper.py").read_text()
    ops = (ROOT / "services/edge-api/app/api/v1/ops.py").read_text()

    assert "build-edge-app-release.sh" in active
    assert "edge-app-v1" in active
    assert "from edge_app_release import" in helper
    assert "from release_engine import" not in helper
    assert "from release_engine import" in legacy_helper
    assert "from edge_app_release import" not in legacy_helper
    assert "/host-release/" not in ops
    assert "license-service" not in script
    assert "HOST_RELEASE" not in script
    assert 'readonly DOCKER_BINARY="/usr/bin/docker"' in script
    assert 'readonly COSIGN_BINARY="/usr/local/libexec/drycontroller/cosign"' in script
    assert 'readonly TRIVY_BINARY="/usr/local/bin/trivy"' in script
    assert "verify-edge-app-toolchain.py" in script
    assert "verify-edge-app-migration-scope.py" in script
    assert "EDGE_APP_APPROVED_DB_BASELINE_COMMIT" in script
    assert "cosign public key fingerprint is not approved" in script
    assert "EDGE_APP_COSIGN_PUBLIC_KEY_SHA256" in active
    assert "EDGE_APP_APPROVED_DB_BASELINE_COMMIT" in active
    assert "test_edge_app_ota_helper.py" in active
    assert "test_edge_app_migration_gate.py" in active
    assert "test_ota_outbox.py" in active
    assert "infra/ota-helper/tests/test_ota_helper.py" not in active
    assert '--operation-id' in rollback
    assert '--expected-active-release-id' in rollback
    assert '--target-release-id' in rollback
    assert "uuid.uuid4" not in rollback
    assert "health_checker=" not in docker_e2e
    assert '"stop", "edge-daq"' in docker_e2e


def test_non_destructive_runtime_retrofit_retires_legacy_runner_without_touching_app_data():
    retrofit = (ROOT / "infra/bootstrap/retrofit-edge-app-runtime.sh").read_text()
    runtime_files = _load_runtime_files()

    assert "systemctl disable --now" in retrofit
    assert "systemctl disable --now drycontroller-ota-helper.service" in retrofit
    assert "ln -s /dev/null" in retrofit
    assert "legacy HostRelease socket remained" in retrofit
    assert "software cosign public key checksum is not approved" in retrofit
    assert "systemctl enable --now drycontroller-tpm-identity-observer.service" in retrofit
    assert "initialize a signed baseline before enabling" in retrofit
    assert "systemctl enable drycontroller-ota-helper.service" not in retrofit
    assert "systemctl enable drycontroller-edge-app-recovery.service" not in retrofit
    assert "installed device identity runtime is unavailable" in retrofit
    assert "python3 -m venv" not in retrofit
    assert "docker pull" not in retrofit
    assert "docker compose" not in retrofit
    assert "initialize-active-release" not in retrofit
    assert not any("host-release" in source for source in runtime_files)


def _load_runtime_files() -> dict[str, tuple[str, int]]:
    path = ROOT / "infra/bootstrap/install-edge-app-runtime.py"
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
    assert module.TOOLS["cosign"]["path"] == Path(
        "/usr/local/libexec/drycontroller/cosign"
    )
    assert module.TOOLS["docker"]["version"] == "29.4.1-1"
    for name, version in (
        ("docker", "29.4.1-1"),
        ("cosign", "v3.1.3"),
        ("trivy", "0.70.0"),
    ):
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
    outputs = iter(
        (
            "29.4.1-1|linux/amd64",
            "GitVersion: v3.1.3\nPlatform: linux/amd64",
            "Version: 0.70.0",
        )
    )
    monkeypatch.setattr(module, "_output", lambda _command: next(outputs))

    receipt = module.verify_release_toolchain(owner_uid=os.getuid(), owner_gid=os.getgid())

    assert receipt["platform"] == "linux/amd64"
    assert set(receipt["tools"]) == {"docker", "cosign", "trivy"}


def _baseline_runner(
    *,
    compose: Path,
    override: Path,
    ids: dict[str, str],
    drift: str | None = None,
) -> tuple[Callable[[list[str]], str], list[list[str]]]:
    images = {
        service: f"registry.test/{service}@{image_id}" for service, image_id in ids.items()
    }
    container_ids = {"edge-api": "a" * 64, "edge-daq": "b" * 64}
    config_hashes = {"edge-api": "c" * 64, "edge-daq": "d" * 64}
    calls: list[list[str]] = []
    rendered = {
        "name": "drycontroller-edge",
        "networks": {"edge-network": {"name": "drycontroller-edge_edge-network"}},
        "services": {
            service: {
                "image": images[service],
                "environment": {"SERVICE": service},
                "networks": {"edge-network": None},
                "ports": [
                    {
                        "host_ip": "127.0.0.1",
                        "target": 8000 if service == "edge-api" else 8080,
                        "published": "18080" if service == "edge-api" else "8082",
                        "protocol": "tcp",
                    }
                ],
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(compose.parent / service),
                        "target": "/data",
                    }
                ],
            }
            for service in ids
        },
        "volumes": {},
    }

    def run(args: list[str]) -> str:
        calls.append(args)
        if args[0].endswith("docker-compose"):
            if args[-3:] == ["config", "--format", "json"]:
                return json.dumps(rendered)
            service = args[-1]
            if args[-3:-1] == ["config", "--hash"]:
                return f"{service} {config_hashes[service]}\n"
            if args[-3:-1] == ["ps", "--quiet"]:
                return container_ids[service] + "\n"
        if args[:3] == ["/usr/bin/docker", "image", "inspect"]:
            reference = args[3]
            service = next(
                name
                for name in ids
                if reference in {ids[name], images[name]}
            )
            return json.dumps(
                [
                    {
                        "Id": ids[service],
                        "Config": {"Env": ["BASE=base"]},
                        "RepoDigests": [images[service]],
                    }
                ]
            )
        if args[:2] == ["/usr/bin/docker", "inspect"]:
            service = next(name for name in ids if args[2] == container_ids[name])
            target = 8000 if service == "edge-api" else 8080
            published = "18080" if service == "edge-api" else "8082"
            return json.dumps(
                [
                    {
                        "Id": container_ids[service],
                        "Image": ids[service],
                        "Config": {
                            "Image": images[service],
                            "Env": ["BASE=base", f"SERVICE={service}"],
                            "Labels": {
                                "com.docker.compose.project": (
                                    "wrong-project" if drift == "project" else "drycontroller-edge"
                                ),
                                "com.docker.compose.service": service,
                                "com.docker.compose.config-hash": (
                                    "e" * 64
                                    if drift == "config-hash"
                                    else config_hashes[service]
                                ),
                                "com.docker.compose.project.working_dir": str(compose.parent),
                                "com.docker.compose.project.config_files": (
                                    f"{compose},{override}"
                                ),
                            },
                        },
                        "Mounts": [
                            {
                                "Type": "bind",
                                "Source": str(compose.parent / service),
                                "Destination": "/data",
                                "RW": True,
                            }
                        ],
                        "NetworkSettings": {
                            "Networks": {
                                (
                                    "wrong-network"
                                    if drift == "network"
                                    else "drycontroller-edge_edge-network"
                                ): {}
                            },
                            "Ports": {
                                f"{target}/tcp": [
                                    {
                                        "HostIp": "127.0.0.1",
                                        "HostPort": published,
                                    }
                                ]
                            },
                        },
                        "State": {"Running": True, "Health": {"Status": "healthy"}},
                    }
                ]
            )
        return ""

    return run, calls


def test_baseline_snapshot_is_read_only_and_requires_canonical_compose(tmp_path: Path):
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n")
    override = tmp_path / "edge-app-images.yml"
    override.write_text("services: {}\n")
    public_key = tmp_path / "cosign.pub"
    public_key.write_text("test")
    output = tmp_path / "manifest.json"
    ids = {
        "edge-api": "sha256:" + "1" * 64,
        "edge-daq": "sha256:" + "2" * 64,
    }
    run, calls = _baseline_runner(compose=compose, override=override, ids=ids)

    manifest = baseline.snapshot(
        output=output,
        release_id="11111111-1111-4111-8111-111111111111",
        sequence=1,
        source_commit="3" * 40,
        expected_ids=ids,
        command_runner=run,
        compose=compose,
        override=override,
        public_key=public_key,
    )

    assert set(manifest["images"]) == {"edge-api", "edge-daq"}
    assert all("pull" not in call and "up" not in call for call in calls)


@pytest.mark.parametrize(
    "drift",
    ["project", "config-hash", "network"],
)
def test_baseline_snapshot_rejects_noncanonical_compose_runtime(
    tmp_path: Path,
    drift: str,
):
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n")
    override = tmp_path / "edge-app-images.yml"
    override.write_text("services: {}\n")
    ids = {
        "edge-api": "sha256:" + "1" * 64,
        "edge-daq": "sha256:" + "2" * 64,
    }
    run, _calls = _baseline_runner(
        compose=compose,
        override=override,
        ids=ids,
        drift=drift,
    )

    with pytest.raises(baseline.ReleaseError, match="canonical Compose"):
        baseline.snapshot(
            output=tmp_path / "manifest.json",
            release_id="11111111-1111-4111-8111-111111111111",
            sequence=1,
            source_commit="3" * 40,
            expected_ids=ids,
            command_runner=run,
            compose=compose,
            override=override,
            public_key=tmp_path / "cosign.pub",
        )
