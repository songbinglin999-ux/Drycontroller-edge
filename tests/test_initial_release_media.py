from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PREPARE = ROOT / "scripts" / "prepare-initial-release.py"
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
HOST_FILES = (
    "docker-compose.yml",
    "infra/ota-helper/drycontroller_ota_helper.py",
    "infra/ota-helper/release_engine.py",
    "infra/ota-helper/ota-apply",
    "infra/ota-helper/initialize-active-release",
    "infra/ota-helper/start-active-release",
)


def _linux_amd64_elf() -> bytes:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    header[18:20] = (62).to_bytes(2, "little")
    return bytes(header)


def _prepare_module():
    spec = importlib.util.spec_from_file_location("prepare_initial_release", PREPARE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _topology(compose_digest: str, service_inventory: list[str]) -> dict[str, object]:
    return {
        "compose_digest": compose_digest,
        "service_inventory": service_inventory,
        "health_contract": "drycontroller.edge-health.v1",
    }


def _topology_digest(compose_digest: str, service_inventory: list[str]) -> str:
    topology = _topology(compose_digest, service_inventory)
    canonical = json.dumps(topology, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def test_prepare_initial_release_verifies_and_packages_the_exact_complete_image_set(tmp_path: Path, monkeypatch):
    source_commit = "a" * 40
    software_public_key = tmp_path / "software-cosign.pub"
    software_public_key.write_bytes(b"software-release-public-key")
    host_public_key = tmp_path / "host-cosign.pub"
    host_public_key.write_bytes(b"host-release-public-key")
    target_cosign_bytes = _linux_amd64_elf()
    compose_digest = "sha256:" + hashlib.sha256((ROOT / "docker-compose.yml").read_bytes()).hexdigest()
    topology = _topology(compose_digest, sorted(SERVICES))
    software_manifest = {
        "schema": "drycontroller.software-release.v1",
        "release_id": "11111111-1111-4111-8111-111111111111",
        "host_release_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "topology_digest": _topology_digest(compose_digest, sorted(SERVICES)),
        "sequence": 1,
        "version": "1.0.0",
        "source_commit": source_commit,
        "registry": "registry.example.test:5443",
        "signing_key_sha256": "sha256:"
        + hashlib.sha256(software_public_key.read_bytes()).hexdigest(),
        "cosign": {
            "version": "v3.1.3",
            "linux_amd64_sha256": "sha256:" + hashlib.sha256(target_cosign_bytes).hexdigest(),
        },
        "services": {
            service: f"registry.example.test:5443/drycontroller/{service}@sha256:{index:064x}"
            for index, service in enumerate(SERVICES, start=1)
        },
        "image_ids": {service: f"sha256:{index:064x}" for index, service in enumerate(SERVICES, start=1)},
    }
    software_canonical = json.dumps(
        software_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    software_release = tmp_path / "software-release.json"
    software_release.write_text(
        json.dumps(
            {
                "id": software_manifest["release_id"],
                "sequence": software_manifest["sequence"],
                "version": software_manifest["version"],
                "source_commit": source_commit,
                "status": "completed",
                "manifest": software_manifest,
                "manifest_digest": "sha256:" + hashlib.sha256(software_canonical).hexdigest(),
                "manifest_signature": base64.b64encode(b"sigstore-bundle").decode(),
            }
        ),
        encoding="utf-8",
    )
    host_bundle_config = b'{"architecture":"amd64"}'
    host_files = []
    for relative in HOST_FILES:
        raw = (ROOT / relative).read_bytes()
        host_files.append(
            {
                "path": relative,
                "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
                "mode": 0o755
                if relative
                in {
                    "infra/ota-helper/ota-apply",
                    "infra/ota-helper/initialize-active-release",
                    "infra/ota-helper/start-active-release",
                }
                else 0o644,
            }
        )
    host_manifest = {
        "schema": "drycontroller.host-release.v1",
        "release_id": software_manifest["host_release_id"],
        "sequence": 1,
        "version": "1.0.0",
        "source_commit": source_commit,
        "registry": software_manifest["registry"],
        "signing_key_sha256": "sha256:"
        + hashlib.sha256(host_public_key.read_bytes()).hexdigest(),
        "cosign": software_manifest["cosign"],
        "bundle": {
            "image": "registry.example.test:5443/drycontroller/host-bundle@sha256:"
            + "f" * 64,
            "image_id": "sha256:" + hashlib.sha256(host_bundle_config).hexdigest(),
        },
        "topology": topology,
        "topology_digest": software_manifest["topology_digest"],
        "software_release_id": software_manifest["release_id"],
        "software_manifest_digest": "sha256:" + hashlib.sha256(software_canonical).hexdigest(),
        "files": host_files,
    }
    host_canonical = json.dumps(
        host_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    host_release = tmp_path / "host-release.json"
    host_release.write_text(
        json.dumps(
            {
                "id": host_manifest["release_id"],
                "sequence": host_manifest["sequence"],
                "version": host_manifest["version"],
                "source_commit": source_commit,
                "status": "completed",
                "manifest": host_manifest,
                "manifest_digest": "sha256:" + hashlib.sha256(host_canonical).hexdigest(),
                "manifest_signature": base64.b64encode(b"host-sigstore-bundle").decode(),
            }
        ),
        encoding="utf-8",
    )
    target_cosign = tmp_path / "cosign-linux-amd64"
    target_cosign.write_bytes(target_cosign_bytes)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "commands.log"
    cosign = fake_bin / "cosign"
    cosign.write_text("#!/bin/sh\nprintf 'cosign %s\\n' \"$*\" >> \"$COMMAND_LOG\"\n", encoding="utf-8")
    docker = fake_bin / "docker"
    docker.write_text(
        r"""#!/bin/sh
printf 'docker %s\n' "$*" >> "$COMMAND_LOG"
if [ "$1 $2" = "image inspect" ]; then
  eval "last=\${$#}"
  printf '%s\n' "${last##*@}"
elif [ "$1 $2" = "image save" ]; then
  printf 'archive' > "$4"
fi
""",
        encoding="utf-8",
    )
    cosign.chmod(0o755)
    docker.chmod(0o755)
    output_dir = tmp_path / "autoinstall"
    software_image_bundle = tmp_path / "bundle-images.tar"
    host_image_bundle = tmp_path / "host-bundle.tar"
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("COMMAND_LOG", str(log))
    module = _prepare_module()
    monkeypatch.setattr(module, "validate_manifest", lambda value: value)
    monkeypatch.setattr(module, "validate_software_manifest", lambda value: value)
    monkeypatch.setattr(module, "validate_host_manifest", lambda value: value)
    expected_ids = {
        reference: software_manifest["image_ids"][service]
        for service, reference in software_manifest["services"].items()
    }
    expected_ids[host_manifest["bundle"]["image"]] = host_manifest["bundle"]["image_id"]
    monkeypatch.setattr(
        module,
        "_portable_image_id",
        lambda reference, _directory: expected_ids[reference],
    )
    host_seed, software_seed = module.prepare(
        software_release=software_release,
        software_public_key=software_public_key,
        host_release=host_release,
        host_public_key=host_public_key,
        cosign_linux_amd64=target_cosign,
        source_commit=source_commit,
        output_dir=output_dir,
        software_image_bundle=software_image_bundle,
        host_image_bundle=host_image_bundle,
    )

    assert software_seed["manifest"] == software_manifest
    assert host_seed["manifest"] == host_manifest
    assert json.loads(
        (output_dir / "software-release-seed.json").read_text(encoding="utf-8")
    ) == software_seed
    assert json.loads((output_dir / "host-release-seed.json").read_text(encoding="utf-8")) == host_seed
    assert (output_dir / "software-release-cosign.pub").read_bytes() == software_public_key.read_bytes()
    assert (output_dir / "host-release-cosign.pub").read_bytes() == host_public_key.read_bytes()
    assert (output_dir / "cosign-linux-amd64").read_bytes() == target_cosign.read_bytes()
    assert software_image_bundle.read_bytes() == b"archive"
    assert host_image_bundle.read_bytes() == b"archive"
    commands = log.read_text(encoding="utf-8")
    assert commands.count("docker pull ") == len(SERVICES) + 1
    assert commands.count("docker tag ") == len(SERVICES)
    assert commands.count("cosign verify --insecure-ignore-tlog=true") == len(SERVICES) + 1
    assert commands.count("cosign verify-blob") == 2
    assert commands.count("docker image save") >= 2
    assert "docker image inspect" not in commands


def test_prepare_initial_release_rejects_wrong_source_before_running_external_tools(tmp_path: Path):
    release = tmp_path / "release.json"
    release.write_text("{}", encoding="utf-8")
    result = subprocess.run(
        [
            "python3",
            str(PREPARE),
            "--software-release",
            str(release),
            "--software-cosign-public-key",
            str(tmp_path / "missing.pub"),
            "--host-release",
            str(tmp_path / "missing-host.json"),
            "--host-cosign-public-key",
            str(tmp_path / "missing-host.pub"),
            "--cosign-linux-amd64",
            str(tmp_path / "missing-cosign"),
            "--source-commit",
            "b" * 40,
            "--output-dir",
            str(tmp_path / "out"),
            "--software-image-bundle",
            str(tmp_path / "images.tar"),
            "--host-image-bundle",
            str(tmp_path / "host-images.tar"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "manifest" in result.stderr


@pytest.mark.parametrize("status_value", [None, "building"])
def test_prepare_initial_release_requires_explicit_completed_status(status_value):
    module = _prepare_module()
    manifest = {
        "schema": "drycontroller.software-release.v1",
        "release_id": "11111111-1111-4111-8111-111111111111",
        "host_release_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "topology_digest": "sha256:" + "c" * 64,
        "sequence": 1,
        "version": "1.0.0",
        "source_commit": "a" * 40,
        "registry": "registry.example.test:5443",
        "signing_key_sha256": "sha256:" + "a" * 64,
        "cosign": {
            "version": "v3.1.3",
            "linux_amd64_sha256": "sha256:4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71",
        },
        "services": {
            service: f"registry.example.test:5443/drycontroller/{service}@sha256:{index:064x}"
            for index, service in enumerate(SERVICES, start=1)
        },
        "image_ids": {service: f"sha256:{index:064x}" for index, service in enumerate(SERVICES, start=1)},
    }
    payload = {
        "manifest": manifest,
        "manifest_digest": module.canonical_manifest_digest(manifest),
        "manifest_signature": base64.b64encode(b"signature").decode("ascii"),
    }
    if status_value is not None:
        payload["status"] = status_value

    with pytest.raises(ValueError, match="not completed"):
        module._normalize_release(payload, "a" * 40)


def test_atomic_write_fsyncs_parent_directory_and_fails_closed_on_directory_fsync(tmp_path: Path, monkeypatch):
    module = _prepare_module()
    target = tmp_path / "seed.json"
    original_fsync = module.os.fsync
    directory_fsyncs = 0

    def track_fsync(descriptor: int) -> None:
        nonlocal directory_fsyncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_fsyncs += 1
        original_fsync(descriptor)

    monkeypatch.setattr(module.os, "fsync", track_fsync)
    module._atomic_write(target, b"first", 0o600)
    assert target.read_bytes() == b"first"
    assert directory_fsyncs == 1

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("injected directory fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(module.os, "fsync", fail_directory_fsync)
    with pytest.raises(OSError, match="injected directory fsync failure"):
        module._atomic_write(target, b"second", 0o600)


def test_durable_bundle_replace_fsyncs_data_before_publish(tmp_path: Path, monkeypatch):
    module = _prepare_module()
    temporary = tmp_path / ".bundle-images.tar.tmp"
    target = tmp_path / "bundle-images.tar"
    temporary.write_bytes(b"new")
    target.write_bytes(b"old")
    original_fsync = module.os.fsync
    fsync_types: list[str] = []

    def track_fsync(descriptor: int) -> None:
        fsync_types.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        original_fsync(descriptor)

    monkeypatch.setattr(module.os, "fsync", track_fsync)
    module._durable_replace(temporary, target)
    assert target.read_bytes() == b"new"
    assert fsync_types == ["file", "directory"]

    temporary.write_bytes(b"unpublished")

    def fail_file_fsync(descriptor: int) -> None:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("injected bundle fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(module.os, "fsync", fail_file_fsync)
    with pytest.raises(OSError, match="injected bundle fsync failure"):
        module._durable_replace(temporary, target)
    assert target.read_bytes() == b"new"
    assert temporary.read_bytes() == b"unpublished"
