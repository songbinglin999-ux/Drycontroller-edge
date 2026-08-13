from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "scripts" / "prepare-host-release.py"
SPEC = importlib.util.spec_from_file_location("prepare_host_release", MODULE_PATH)
assert SPEC and SPEC.loader
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


def test_stage_and_manifest_are_fixed_signed_host_content(tmp_path):
    rootfs = tmp_path / "bundle" / "host-release"
    topology_file = tmp_path / "topology.json"
    topology = prepare.stage(ROOT, rootfs, topology_file)

    compose = (rootfs / "docker-compose.yml").read_text(encoding="utf-8")
    assert "\n    build:" not in compose
    assert "/var/lib/drycontroller/configs-runtime" in compose
    assert "/var/log/drycontroller" in compose
    assert not (rootfs / "configs/keys/cosign.pub").exists()
    assert not (rootfs / "infra/discovery/bootstrap.json").exists()
    assert not (rootfs / "infra/ota-helper/drycontroller-ota-helper.service").exists()
    assert sorted(topology["service_inventory"]) == topology["service_inventory"]

    host_id = "11111111-1111-4111-8111-111111111111"
    software_id = "22222222-2222-4222-8222-222222222222"
    source_commit = "a" * 40
    topology_digest = prepare.canonical_topology_digest(topology)
    services = {
        service: f"registry.example.test:5443/drycontroller/{service}@sha256:{index:064x}"
        for index, service in enumerate(topology["service_inventory"], start=1)
    }
    image_ids = {
        service: f"sha256:{index + 100:064x}"
        for index, service in enumerate(topology["service_inventory"], start=1)
    }
    software = {
        "schema": "drycontroller.software-release.v1",
        "release_id": software_id,
        "host_release_id": host_id,
        "topology_digest": topology_digest,
        "sequence": 7,
        "version": "2.0.0",
        "source_commit": source_commit,
        "registry": "registry.example.test:5443",
        "signing_key_sha256": "sha256:" + "3" * 64,
        "cosign": prepare.COSIGN_TOOLCHAIN,
        "services": services,
        "image_ids": image_ids,
    }
    host_release_file = tmp_path / "host-release.json"
    host_release_file.write_text(
        json.dumps(
            {
                "id": host_id,
                "sequence": 3,
                "version": "2.0.0",
                "source_commit": source_commit,
                "status": "building",
            }
        ),
        encoding="utf-8",
    )
    software_release_file = tmp_path / "software-release.json"
    software_release_file.write_text(
        json.dumps(
            {
                "id": software_id,
                "status": "completed",
                "manifest": software,
                "manifest_digest": prepare.canonical_digest(software),
            }
        ),
        encoding="utf-8",
    )
    public_key = tmp_path / "host.pub"
    public_key.write_bytes(b"separate-host-release-public-key")

    result = prepare.manifest(
        root=rootfs,
        topology_file=topology_file,
        host_release_file=host_release_file,
        software_release_file=software_release_file,
        host_public_key=public_key,
        bundle_image="registry.example.test:5443/drycontroller/host-bundle@sha256:" + "4" * 64,
        bundle_image_id="sha256:" + "5" * 64,
    )

    assert prepare.validate_host_manifest(result) == result
    assert result["software_release_id"] == software_id
    assert result["topology_digest"] == topology_digest
    paths = {record["path"] for record in result["files"]}
    assert "infra/ota-helper/release_engine.py" in paths
    assert not any(path.startswith("configs/keys/") for path in paths)


def test_host_builder_uses_portable_image_config_digest():
    script = (ROOT / "scripts/ci/build-host-release.sh").read_text(encoding="utf-8")

    assert "docker image save -o \"${bundle_verification_archive}\"" in script
    assert "from release_engine import docker_archive_config_digest" in script
    assert "bundle_image_id=\"$(docker image inspect" not in script
