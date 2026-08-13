#!/usr/bin/env python3
"""Build deterministic HostRelease files and the paired canonical manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_DIR = ROOT / "infra" / "host-release-runner"
if str(RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(RUNNER_DIR))

from host_release_engine import (  # noqa: E402
    COSIGN_TOOLCHAIN,
    HOST_HEALTH_CONTRACT,
    assert_pair,
    canonical_digest,
    canonical_json_bytes,
    canonical_topology_digest,
    validate_host_manifest,
    validate_software_manifest,
)


STATIC_DIRS = (
    "configs",
    "infra/alertmanager",
    "infra/mosquitto",
    "infra/postgres",
    "infra/prometheus",
)
HELPER_FILES = (
    "infra/ota-helper/drycontroller_ota_helper.py",
    "infra/ota-helper/release_engine.py",
    "infra/ota-helper/ota-apply",
    "infra/ota-helper/initialize-active-release",
    "infra/ota-helper/start-active-release",
)
EXECUTABLE_FILES = {
    "infra/mosquitto/entrypoint.sh",
    "infra/ota-helper/ota-apply",
    "infra/ota-helper/initialize-active-release",
    "infra/ota-helper/start-active-release",
}
SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
BUILD_BLOCK = re.compile(r"^    build:\s*(?:#.*)?$")
SERVICE_LINE = re.compile(r"^  ([a-z0-9][a-z0-9._-]{0,62}):\s*(?:#.*)?$")


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _read_json(path: Path, maximum: int = 4 * 1024 * 1024) -> object:
    if not path.is_file() or path.stat().st_size > maximum:
        raise ValueError(f"JSON input missing or too large: {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def _production_compose(raw: bytes) -> bytes:
    text = raw.decode("utf-8")
    output: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        indent = len(line) - len(line.lstrip(" "))
        if skipping:
            if not line.strip() or indent > 4:
                continue
            skipping = False
        if BUILD_BLOCK.fullmatch(line.rstrip("\r\n")):
            skipping = True
            continue
        output.append(line)
    result = "".join(output).encode("utf-8")
    if b"\n    build:" in result:
        raise ValueError("canonical HostRelease Compose still contains build")
    return result


def _service_inventory(compose: bytes) -> list[str]:
    services: list[str] = []
    inside = False
    for line in compose.decode("utf-8").splitlines():
        if line == "services:":
            inside = True
            continue
        if not inside:
            continue
        if line and not line.startswith((" ", "#")):
            break
        match = SERVICE_LINE.fullmatch(line)
        if match:
            services.append(match.group(1))
    if not services or any(SERVICE_NAME.fullmatch(service) is None for service in services):
        raise ValueError("canonical HostRelease Compose has no valid services")
    if len(services) != len(set(services)):
        raise ValueError("canonical HostRelease Compose contains duplicate services")
    return sorted(services)


def _copy_file(source_root: Path, output_root: Path, relative: str) -> None:
    source = source_root / relative
    metadata = source.lstat()
    if not stat.S_ISREG(metadata.st_mode) or source.is_symlink():
        raise ValueError(f"HostRelease source must be a regular file: {relative}")
    destination = output_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    destination.chmod(0o755 if relative in EXECUTABLE_FILES else 0o644)


def stage(source_root: Path, output_root: Path, topology_output: Path) -> dict[str, object]:
    if output_root.exists() and any(output_root.iterdir()):
        raise ValueError("HostRelease staging directory must be empty")
    output_root.mkdir(parents=True, exist_ok=True)
    compose = _production_compose((source_root / "docker-compose.yml").read_bytes())
    (output_root / "docker-compose.yml").write_bytes(compose)
    (output_root / "docker-compose.yml").chmod(0o644)

    for directory in STATIC_DIRS:
        source_directory = source_root / directory
        for source in sorted(source_directory.rglob("*")):
            if source.is_dir():
                continue
            relative = source.relative_to(source_root).as_posix()
            if relative.startswith("configs/keys/"):
                continue
            _copy_file(source_root, output_root, relative)
    for relative in HELPER_FILES:
        _copy_file(source_root, output_root, relative)

    topology = {
        "compose_digest": _sha256(compose),
        "service_inventory": _service_inventory(compose),
        "health_contract": HOST_HEALTH_CONTRACT,
    }
    topology_output.parent.mkdir(parents=True, exist_ok=True)
    topology_output.write_bytes(canonical_json_bytes(topology))
    topology_output.chmod(0o600)
    return topology


def _file_manifest(root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise ValueError("HostRelease staging contains non-regular content")
        data = path.read_bytes()
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": _sha256(data),
                "size": len(data),
                "mode": stat.S_IMODE(metadata.st_mode),
            }
        )
    return records


def manifest(
    *,
    root: Path,
    topology_file: Path,
    host_release_file: Path,
    software_release_file: Path,
    host_public_key: Path,
    bundle_image: str,
    bundle_image_id: str,
) -> dict[str, object]:
    host_release = _read_json(host_release_file)
    software_release = _read_json(software_release_file)
    topology = _read_json(topology_file)
    if not isinstance(host_release, dict) or host_release.get("status") != "building":
        raise ValueError("HostRelease Cloud reservation must be building")
    if not isinstance(software_release, dict) or software_release.get("status") != "completed":
        raise ValueError("paired SoftwareRelease must be completed")
    software = validate_software_manifest(software_release.get("manifest"))
    if software_release.get("manifest_digest") != canonical_digest(software):
        raise ValueError("paired SoftwareRelease digest mismatch")
    if not isinstance(topology, dict):
        raise ValueError("HostRelease topology invalid")
    topology_digest = canonical_topology_digest(topology)
    if (
        software["host_release_id"] != host_release.get("id")
        or software["topology_digest"] != topology_digest
        or sorted(software["services"]) != topology.get("service_inventory")
    ):
        raise ValueError("HostRelease and SoftwareRelease pair mismatch")
    public_key = host_public_key.read_bytes()
    if not public_key or len(public_key) > 64 * 1024:
        raise ValueError("HostRelease public key missing or too large")
    result = {
        "schema": "drycontroller.host-release.v1",
        "release_id": host_release.get("id"),
        "sequence": host_release.get("sequence"),
        "version": host_release.get("version"),
        "source_commit": host_release.get("source_commit"),
        "registry": software["registry"],
        "signing_key_sha256": _sha256(public_key),
        "cosign": COSIGN_TOOLCHAIN,
        "bundle": {"image": bundle_image, "image_id": bundle_image_id},
        "topology": topology,
        "topology_digest": topology_digest,
        "software_release_id": software["release_id"],
        "software_manifest_digest": canonical_digest(software),
        "files": _file_manifest(root),
    }
    validate_host_manifest(result)
    assert_pair(result, software)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    stage_parser = subparsers.add_parser("stage")
    stage_parser.add_argument("--source-root", type=Path, default=ROOT)
    stage_parser.add_argument("--output-root", type=Path, required=True)
    stage_parser.add_argument("--topology-output", type=Path, required=True)
    manifest_parser = subparsers.add_parser("manifest")
    manifest_parser.add_argument("--root", type=Path, required=True)
    manifest_parser.add_argument("--topology", type=Path, required=True)
    manifest_parser.add_argument("--host-release", type=Path, required=True)
    manifest_parser.add_argument("--software-release", type=Path, required=True)
    manifest_parser.add_argument("--host-public-key", type=Path, required=True)
    manifest_parser.add_argument("--bundle-image", required=True)
    manifest_parser.add_argument("--bundle-image-id", required=True)
    manifest_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "stage":
            result = stage(args.source_root.resolve(), args.output_root, args.topology_output)
            print(canonical_topology_digest(result))
        else:
            result = manifest(
                root=args.root,
                topology_file=args.topology,
                host_release_file=args.host_release,
                software_release_file=args.software_release,
                host_public_key=args.host_public_key,
                bundle_image=args.bundle_image,
                bundle_image_id=args.bundle_image_id,
            )
            args.output.write_bytes(canonical_json_bytes(result))
            os.chmod(args.output, 0o600)
            print(canonical_digest(result))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"HostRelease preparation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
