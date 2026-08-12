from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path


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


def test_prepare_initial_release_verifies_and_packages_the_exact_complete_image_set(tmp_path: Path):
    source_commit = "a" * 40
    public_key = tmp_path / "cosign.pub"
    public_key.write_bytes(b"factory-release-public-key")
    manifest = {
        "schema": "drycontroller.software-release.v1",
        "release_id": "11111111-1111-4111-8111-111111111111",
        "sequence": 1,
        "version": "1.0.0",
        "source_commit": source_commit,
        "registry": "registry.example.test:5443",
        "signing_key_sha256": "sha256:" + hashlib.sha256(public_key.read_bytes()).hexdigest(),
        "services": {
            service: f"registry.example.test:5443/drycontroller/{service}@sha256:{index:064x}"
            for index, service in enumerate(SERVICES, start=1)
        },
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
    target_cosign.write_bytes(_linux_amd64_elf())
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
  printf '["%s"]\n' "$last"
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
    environment = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "COMMAND_LOG": str(log)}

    subprocess.run(
        [
            "python3",
            str(PREPARE),
            "--software-release",
            str(release),
            "--cosign-public-key",
            str(public_key),
            "--cosign-linux-amd64",
            str(target_cosign),
            "--source-commit",
            source_commit,
            "--output-dir",
            str(output_dir),
            "--image-bundle",
            str(image_bundle),
        ],
        check=True,
        env=environment,
    )

    seed = json.loads((output_dir / "software-release-seed.json").read_text(encoding="utf-8"))
    assert seed["manifest"] == manifest
    assert (output_dir / "cosign.pub").read_bytes() == public_key.read_bytes()
    assert (output_dir / "cosign-linux-amd64").read_bytes() == target_cosign.read_bytes()
    assert image_bundle.read_bytes() == b"archive"
    commands = log.read_text(encoding="utf-8")
    assert commands.count("docker pull ") == len(SERVICES)
    assert commands.count("cosign verify --insecure-ignore-tlog=true") == len(SERVICES)
    assert "cosign verify-blob" in commands
    assert "docker image save" in commands


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
