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


def _topology_digest(compose_digest: str, service_inventory: list[str]) -> str:
    topology = {"compose_digest": compose_digest, "service_inventory": service_inventory}
    canonical = json.dumps(topology, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def test_prepare_initial_release_verifies_and_packages_the_exact_complete_image_set(tmp_path: Path, monkeypatch):
    source_commit = "a" * 40
    public_key = tmp_path / "cosign.pub"
    public_key.write_bytes(b"factory-release-public-key")
    target_cosign_bytes = _linux_amd64_elf()
    compose_digest = "sha256:" + hashlib.sha256((ROOT / "docker-compose.yml").read_bytes()).hexdigest()
    manifest = {
        "schema": "drycontroller.software-release.v1",
        "release_id": "11111111-1111-4111-8111-111111111111",
        "host_release_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "topology_digest": _topology_digest(compose_digest, sorted(SERVICES)),
        "sequence": 1,
        "version": "1.0.0",
        "source_commit": source_commit,
        "registry": "registry.example.test:5443",
        "signing_key_sha256": "sha256:" + hashlib.sha256(public_key.read_bytes()).hexdigest(),
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
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    release = tmp_path / "release.json"
    release.write_text(
        json.dumps(
            {
                "id": manifest["release_id"],
                "sequence": manifest["sequence"],
                "version": manifest["version"],
                "source_commit": source_commit,
                "status": "completed",
                "manifest": manifest,
                "manifest_digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
                "manifest_signature": base64.b64encode(b"sigstore-bundle").decode(),
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
    image_bundle = tmp_path / "bundle-images.tar"
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("COMMAND_LOG", str(log))
    module = _prepare_module()
    monkeypatch.setattr(module, "validate_manifest", lambda value: value)
    expected_ids = {
        reference: manifest["image_ids"][service]
        for service, reference in manifest["services"].items()
    }
    monkeypatch.setattr(
        module,
        "_portable_image_id",
        lambda reference, _directory: expected_ids[reference],
    )
    host_active = tmp_path / "host-active.json"
    host_active.write_text(
        json.dumps(
            {
                "schema": "drycontroller.host-active.v1",
                "host_release_id": manifest["host_release_id"],
                "host_release_manifest_digest": "sha256:" + "d" * 64,
                "topology_digest": manifest["topology_digest"],
                "compose_digest": compose_digest,
                "service_inventory": sorted(manifest["services"]),
            }
        ),
        encoding="utf-8",
    )

    module.prepare(
        software_release=release,
        public_key=public_key,
        cosign_linux_amd64=target_cosign,
        source_commit=source_commit,
        host_active_file=host_active,
        output_dir=output_dir,
        image_bundle=image_bundle,
    )

    seed = json.loads((output_dir / "software-release-seed.json").read_text(encoding="utf-8"))
    assert seed["manifest"] == manifest
    assert (output_dir / "host-active.json").read_bytes() == host_active.read_bytes()
    assert (output_dir / "cosign.pub").read_bytes() == public_key.read_bytes()
    assert (output_dir / "cosign-linux-amd64").read_bytes() == target_cosign.read_bytes()
    assert image_bundle.read_bytes() == b"archive"
    commands = log.read_text(encoding="utf-8")
    assert commands.count("docker pull ") == len(SERVICES)
    assert commands.count("docker tag ") == len(SERVICES)
    assert commands.count("cosign verify --insecure-ignore-tlog=true") == len(SERVICES)
    assert "cosign verify-blob" in commands
    assert "docker image save" in commands
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
            "--cosign-public-key",
            str(tmp_path / "missing.pub"),
            "--cosign-linux-amd64",
            str(tmp_path / "missing-cosign"),
            "--source-commit",
            "b" * 40,
            "--host-active",
            str(tmp_path / "missing-host-active.json"),
            "--output-dir",
            str(tmp_path / "out"),
            "--image-bundle",
            str(tmp_path / "images.tar"),
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
