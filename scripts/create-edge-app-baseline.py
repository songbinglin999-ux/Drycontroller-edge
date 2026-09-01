#!/usr/bin/env python3
"""Create a signed-seed input from the approved live two-service runtime."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra" / "ota-helper"))

from edge_app_release import (  # noqa: E402
    EDGE_SERVICES,
    ReleaseError,
    canonical_json_bytes,
    canonical_manifest_digest,
    deployment_digest,
    prove_compose_runtime,
    read_device_line_id,
    validate_manifest,
    verify_manifest_signature,
)


IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_REFERENCE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
COMPOSE = Path("/opt/drycontroller-host/current/docker-compose.yml")
IMAGE_OVERRIDE = Path("/var/lib/drycontroller-ota/edge-app-images.yml")
DEVICE = Path("/var/lib/drycontroller/device.json")
PUBLIC_KEY = Path("/etc/drycontroller/keys/cosign.pub")


def _run(args: list[str]) -> str:
    completed = subprocess.run(
        args,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise ReleaseError(f"command failed: {Path(args[0]).name}")
    return completed.stdout


def snapshot(
    *,
    output: Path,
    release_id: str,
    sequence: int,
    source_commit: str,
    expected_ids: dict[str, str],
    command_runner=_run,
    compose: Path = COMPOSE,
    override: Path = IMAGE_OVERRIDE,
    device: Path = DEVICE,
    public_key: Path = PUBLIC_KEY,
) -> dict:
    if str(uuid.UUID(release_id)) != release_id or sequence < 1:
        raise ReleaseError("baseline identity invalid")
    images: dict[str, str] = {}
    for service in EDGE_SERVICES:
        if IMAGE_ID.fullmatch(expected_ids.get(service, "")) is None:
            raise ReleaseError("approved baseline image ID invalid")
        try:
            image = json.loads(
                command_runner(["/usr/bin/docker", "image", "inspect", expected_ids[service]])
            )[0]
            references = sorted(
                set(
                    reference
                    for reference in image["RepoDigests"]
                    if isinstance(reference, str) and IMAGE_REFERENCE.fullmatch(reference)
                )
            )
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ReleaseError("live baseline digest inspection invalid") from exc
        if len(references) != 1:
            raise ReleaseError("live baseline immutable digest is ambiguous")
        command_runner(
            [
                "/usr/local/bin/cosign",
                "verify",
                "--insecure-ignore-tlog=true",
                "--key",
                str(public_key),
                references[0],
            ]
        )
        images[service] = references[0]
    prove_compose_runtime(
        project=compose.parent,
        compose=compose,
        override=override,
        images=images,
        expected_line_id=read_device_line_id(device),
        command_runner=command_runner,
        expected_image_ids=expected_ids,
    )
    manifest = validate_manifest(
        {
            "schema": "drycontroller.app-release.v2",
            "profile": "edge-app-v1",
            "release_id": release_id,
            "sequence": sequence,
            "source_commit": source_commit,
            "deployment_digest": deployment_digest(compose),
            "images": images,
        }
    )
    output.write_bytes(canonical_json_bytes(manifest))
    return manifest


def seed(*, manifest_path: Path, bundle_path: Path, output: Path, public_key: Path = PUBLIC_KEY) -> dict:
    manifest = validate_manifest(json.loads(manifest_path.read_text()))
    encoded_bundle = base64.b64encode(bundle_path.read_bytes()).decode()
    verify_manifest_signature(manifest, encoded_bundle, public_key)
    value = {
        "manifest": manifest,
        "manifest_digest": canonical_manifest_digest(manifest),
        "manifest_signature": encoded_bundle,
    }
    output.write_bytes(canonical_json_bytes(value))
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("snapshot")
    capture.add_argument("--output", required=True, type=Path)
    capture.add_argument("--release-id", required=True)
    capture.add_argument("--sequence", required=True, type=int)
    capture.add_argument("--source-commit", required=True)
    capture.add_argument("--expected-edge-api-id", required=True)
    capture.add_argument("--expected-edge-daq-id", required=True)
    make_seed = commands.add_parser("seed")
    make_seed.add_argument("--manifest", required=True, type=Path)
    make_seed.add_argument("--bundle", required=True, type=Path)
    make_seed.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            snapshot(
                output=args.output,
                release_id=args.release_id,
                sequence=args.sequence,
                source_commit=args.source_commit,
                expected_ids={
                    "edge-api": args.expected_edge_api_id,
                    "edge-daq": args.expected_edge_daq_id,
                },
            )
        else:
            seed(manifest_path=args.manifest, bundle_path=args.bundle, output=args.output)
    except (OSError, ValueError, json.JSONDecodeError, ReleaseError) as exc:
        print(f"edge-app-v1 baseline failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
