#!/usr/bin/env python3
"""Small Cloud handoff for the fixed edge-app-v1 release workflow."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import sys
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra" / "ota-helper"))

from edge_app_release import (  # noqa: E402
    EDGE_SERVICES,
    canonical_json_bytes,
    canonical_manifest_digest,
    deployment_digest,
    validate_manifest,
)


def _request(path: str, *, method: str, body: dict | None = None) -> dict:
    base = os.environ["CLOUD_OPS_RELEASE_API_URL"].rstrip("/")
    request = urllib.request.Request(
        base + path,
        data=canonical_json_bytes(body) if body is not None else None,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": os.environ["CLOUD_OPS_RELEASE_API_KEY"],
        },
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.loads(response.read(1024 * 1024))
    if not isinstance(value, dict):
        raise SystemExit("Cloud returned an invalid release response")
    return value


def create(output: Path) -> None:
    value = _request(
        "/api/v1/software-releases",
        method="POST",
        body={
            "version": os.environ["RELEASE_VERSION"],
            "source_commit": os.environ["RELEASE_SOURCE_COMMIT"],
        },
    )
    if (
        value.get("status") != "building"
        or value.get("profile") != "edge-app-v1"
        or value.get("schema_version") != 2
    ):
        raise SystemExit("Cloud did not create an edge-app-v1 release")
    output.write_bytes(canonical_json_bytes({"id": value["id"], "sequence": value["sequence"]}))


def manifest(metadata_path: Path, images_path: Path, output: Path) -> None:
    metadata = json.loads(metadata_path.read_text())
    images: dict[str, str] = {}
    for line in images_path.read_text().splitlines():
        service, reference = line.split("\t", 1)
        images[service] = reference
    value = validate_manifest(
        {
            "schema": "drycontroller.app-release.v2",
            "profile": "edge-app-v1",
            "release_id": metadata["id"],
            "sequence": metadata["sequence"],
            "source_commit": os.environ["RELEASE_SOURCE_COMMIT"],
            "deployment_digest": deployment_digest(ROOT / "docker-compose.yml"),
            "images": images,
        }
    )
    output.write_bytes(canonical_json_bytes(value))


def complete(manifest_path: Path, bundle_path: Path) -> None:
    value = validate_manifest(json.loads(manifest_path.read_text()))
    response = _request(
        f"/api/v1/software-releases/{value['release_id']}/complete",
        method="POST",
        body={
            "manifest": value,
            "manifest_signature": base64.b64encode(bundle_path.read_bytes()).decode(),
        },
    )
    if (
        response.get("status") != "completed"
        or response.get("manifest_digest") != canonical_manifest_digest(value)
        or response.get("profile") != "edge-app-v1"
    ):
        raise SystemExit("Cloud did not complete the immutable edge-app-v1 release")


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inventory")
    create_parser = commands.add_parser("create")
    create_parser.add_argument("output", type=Path)
    manifest_parser = commands.add_parser("manifest")
    manifest_parser.add_argument("metadata", type=Path)
    manifest_parser.add_argument("images", type=Path)
    manifest_parser.add_argument("output", type=Path)
    complete_parser = commands.add_parser("complete")
    complete_parser.add_argument("manifest", type=Path)
    complete_parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    if args.command == "inventory":
        print("\n".join(EDGE_SERVICES))
    elif args.command == "create":
        create(args.output)
    elif args.command == "manifest":
        manifest(args.metadata, args.images, args.output)
    else:
        complete(args.manifest, args.bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
