#!/usr/bin/env python3
"""Behavioral steps for the signed SoftwareRelease shell entrypoint."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra" / "ota-helper"))
sys.path.insert(0, str(ROOT / "infra" / "host-release-runner"))

from host_release_engine import (  # noqa: E402
    assert_pair,
    canonical_digest as host_canonical_digest,
    validate_host_manifest,
    validate_software_manifest,
)
from release_engine import (  # noqa: E402
    COSIGN_TOOLCHAIN,
    canonical_manifest_bytes,
    docker_archive_config_digest,
    validate_manifest,
)


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
SOURCE_IMAGES = {
    "alertmanager": (
        "prom/alertmanager:v0.33.1@"
        "sha256:a89f8d4520954079275441eecdb71444328bd90633dd4eddfc33b9ed657f349b"
    ),
    "mosquitto": (
        "eclipse-mosquitto:2.0.22@"
        "sha256:54c90ecc78645241b6aa272b2a5ac8fc20b0eaf02cc4dd431c0cc8d2fd4447dd"
    ),
    "mosquitto-init": (
        "eclipse-mosquitto:2.0.22@"
        "sha256:54c90ecc78645241b6aa272b2a5ac8fc20b0eaf02cc4dd431c0cc8d2fd4447dd"
    ),
    "prometheus": (
        "prom/prometheus:v3.13.0@"
        "sha256:0e698e35e50d1ddc2d11a4a55b089fe62eb71358a5c204dfafd21bdf8ffe04b8"
    ),
}
RELEASE_DOCKERFILES = {
    "influxdb": "infra/release-images/influxdb/Dockerfile",
    "postgres": "infra/release-images/postgres/Dockerfile",
}


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _fetch(name: str, environ: Mapping[str, str]) -> list[Any]:
    request = urllib.request.Request(
        environ["CLOUD_OPS_RELEASE_API_URL"].rstrip("/") + f"/api/v1/{name}",
        headers={"X-API-Key": environ["CLOUD_OPS_RELEASE_API_KEY"]},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read(4 * 1024 * 1024))
    if not isinstance(payload, list):
        raise SystemExit(f"Cloud returned an invalid {name} list")
    return payload


def _index_rows(rows: list[Any], label: str) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    sequences: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise SystemExit(f"Cloud returned an invalid {label} row")
        sequence = row.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise SystemExit(f"Cloud returned an invalid {label} sequence")
        release_id = row.get("id")
        if not isinstance(release_id, str) or not release_id:
            raise SystemExit(f"Cloud returned an invalid {label} id")
        if sequence in sequences or release_id in by_id:
            raise SystemExit(f"Cloud returned duplicate {label} identity")
        sequences.add(sequence)
        by_id[release_id] = row
    return by_id


def _decode_signature(row: Mapping[str, Any], label: str) -> bytes:
    try:
        signature = base64.b64decode(row.get("manifest_signature", ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SystemExit(f"completed {label} signature is invalid") from exc
    if not signature:
        raise SystemExit(f"completed {label} signature is missing")
    return signature


def _validate_software_row(
    row: Mapping[str, Any], service_inventory: set[str]
) -> dict[str, Any]:
    manifest = validate_manifest(row.get("manifest"))
    validate_software_manifest(manifest)
    expected_digest = "sha256:" + hashlib.sha256(
        canonical_manifest_bytes(manifest)
    ).hexdigest()
    if (
        row.get("status") != "completed"
        or row.get("id") != manifest["release_id"]
        or row.get("sequence") != manifest["sequence"]
        or row.get("version") != manifest["version"]
        or row.get("source_commit") != manifest["source_commit"]
        or row.get("manifest_digest") != expected_digest
        or set(manifest["services"]) != service_inventory
    ):
        raise SystemExit("completed SoftwareRelease identity is invalid")
    _decode_signature(row, "SoftwareRelease")
    return manifest


def _validate_host_row(row: Mapping[str, Any]) -> dict[str, Any]:
    manifest = validate_host_manifest(row.get("manifest"))
    if (
        row.get("status") != "completed"
        or row.get("id") != manifest["release_id"]
        or row.get("sequence") != manifest["sequence"]
        or row.get("version") != manifest["version"]
        or row.get("source_commit") != manifest["source_commit"]
        or row.get("manifest_digest") != host_canonical_digest(manifest)
        or row.get("software_release_id") != manifest["software_release_id"]
    ):
        raise SystemExit("completed HostRelease identity is invalid")
    _decode_signature(row, "HostRelease")
    return manifest


def resolve_predecessor(
    predecessor_path: Path,
    resumable_path: Path,
    *,
    environ: Mapping[str, str] | None = None,
    fetcher: Callable[[str], list[Any]] | None = None,
) -> None:
    env = _environment(environ)
    fetch_rows = fetcher or (lambda name: _fetch(name, env))
    software_rows = fetch_rows("software-releases")
    host_rows = fetch_rows("host-releases")
    software_by_id = _index_rows(software_rows, "SoftwareRelease")
    host_by_id = _index_rows(host_rows, "HostRelease")
    service_inventory = set(SERVICES)
    completed_software: dict[str, dict[str, Any]] = {}
    completed_hosts: dict[str, dict[str, Any]] = {}
    for row in software_rows:
        if row.get("status") == "completed":
            completed_software[row["id"]] = _validate_software_row(
                row, service_inventory
            )
    for row in host_rows:
        if row.get("status") == "completed":
            completed_hosts[row["id"]] = _validate_host_row(row)

    for host_manifest in completed_hosts.values():
        software_row = software_by_id.get(host_manifest["software_release_id"])
        if software_row is None or software_row.get("status") != "completed":
            raise SystemExit("completed HostRelease has no completed SoftwareRelease")
        assert_pair(host_manifest, completed_software[software_row["id"]])

    current_host = host_by_id.get(env["HOST_RELEASE_ID"])
    current_host_is_building = (
        isinstance(current_host, dict)
        and current_host.get("status") == "building"
        and current_host.get("source_commit") == env["RELEASE_SOURCE_COMMIT"]
    )
    resumable: list[dict[str, Any]] = []
    pairs: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for row in software_rows:
        if row.get("status") != "completed":
            continue
        manifest = completed_software[row["id"]]
        if (
            current_host_is_building
            and row.get("version") == env["RELEASE_VERSION"]
            and row.get("source_commit") == env["RELEASE_SOURCE_COMMIT"]
            and manifest["host_release_id"] == env["HOST_RELEASE_ID"]
            and manifest["topology_digest"] == env["TOPOLOGY_DIGEST"]
            and manifest["registry"] == env["HARBOR_HOST"]
            and manifest["signing_key_sha256"] == env["SIGNING_KEY_SHA256"]
        ):
            resumable.append(row)
        host_row = host_by_id.get(manifest["host_release_id"])
        if host_row is None or host_row.get("status") != "completed":
            continue
        host_manifest = completed_hosts[host_row["id"]]
        if host_manifest["software_release_id"] != row["id"]:
            continue
        assert_pair(host_manifest, manifest)
        pairs.append((row["sequence"], row, host_row))

    if len(resumable) > 1:
        raise SystemExit("multiple resumable SoftwareReleases found")
    pairs.sort(key=lambda item: item[0], reverse=True)
    predecessor = None
    if pairs:
        predecessor = dict(pairs[0][1])
        predecessor["paired_host_release"] = pairs[0][2]
    predecessor_path.write_text(
        json.dumps(predecessor, separators=(",", ":")), encoding="utf-8"
    )
    resumable_path.write_text(
        json.dumps(resumable[0], separators=(",", ":")) if resumable else "",
        encoding="utf-8",
    )


def resolve_changed_services(
    predecessor_path: Path,
    output_path: Path,
    *,
    environ: Mapping[str, str] | None = None,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> None:
    env = _environment(environ)
    predecessor = json.loads(predecessor_path.read_text(encoding="utf-8"))
    changed = set(SERVICES)
    if predecessor is not None:
        manifest = validate_manifest(predecessor.get("manifest"))
        source_commit = predecessor.get("source_commit")
        identity_matches = (
            predecessor.get("id") == manifest["release_id"]
            and predecessor.get("sequence") == manifest["sequence"]
            and predecessor.get("version") == manifest["version"]
            and source_commit == manifest["source_commit"]
            and predecessor.get("manifest_digest")
            and predecessor.get("manifest_signature")
        )
        can_inherit = (
            identity_matches
            and manifest["registry"] == env["HARBOR_HOST"]
            and manifest["signing_key_sha256"] == env["SIGNING_KEY_SHA256"]
            and manifest["host_release_id"] == env["HOST_RELEASE_ID"]
            and manifest["topology_digest"] == env["TOPOLOGY_DIGEST"]
        )
        if can_inherit:
            current_commit = env["RELEASE_SOURCE_COMMIT"]
            try:
                run(
                    ["git", "merge-base", "--is-ancestor", source_commit, current_commit],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                result = run(
                    ["git", "diff", "--name-only", "-z", source_commit, current_commit, "--"],
                    check=True,
                    stdout=subprocess.PIPE,
                )
            except (OSError, subprocess.CalledProcessError):
                pass
            else:
                changed = set()
                for raw_path in result.stdout.split(b"\0"):
                    if not raw_path:
                        continue
                    path = raw_path.decode("utf-8")
                    parts = path.split("/")
                    if len(parts) >= 3 and parts[0] == "services" and parts[1] in SERVICES:
                        changed.add(parts[1])
                    elif parts[0] in {"docs", "tests", ".gitea"}:
                        continue
                    else:
                        changed = set(SERVICES)
                        break
    output_path.write_text(
        "".join(f"{service}\n" for service in SERVICES if service in changed),
        encoding="utf-8",
    )


def write_docker_auth(path: Path, *, environ: Mapping[str, str] | None = None) -> None:
    env = _environment(environ)
    auth = base64.b64encode(
        f"{env['HARBOR_USER']}:{env['HARBOR_PASSWORD']}".encode()
    ).decode()
    path.write_text(
        json.dumps(
            {"auths": {env["HARBOR_HOST"]: {"auth": auth}}},
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def create_cloud_release(
    output_path: Path, *, environ: Mapping[str, str] | None = None
) -> None:
    env = _environment(environ)
    body = json.dumps(
        {"version": env["RELEASE_VERSION"], "source_commit": env["RELEASE_SOURCE_COMMIT"]},
        separators=(",", ":"),
    ).encode()
    request = urllib.request.Request(
        env["CLOUD_OPS_RELEASE_API_URL"].rstrip("/") + "/api/v1/software-releases",
        data=body,
        headers={"Content-Type": "application/json", "X-API-Key": env["CLOUD_OPS_RELEASE_API_KEY"]},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read(1024 * 1024))
    if payload.get("status") != "building" or not payload.get("id") or int(payload.get("sequence") or 0) < 1:
        raise SystemExit("Cloud did not create a building SoftwareRelease")
    output_path.write_text(
        json.dumps({"id": payload["id"], "sequence": int(payload["sequence"])}, separators=(",", ":")),
        encoding="utf-8",
    )


def portable_image_id(archive: Path) -> str:
    return docker_archive_config_digest(archive)


def load_inherited_images(
    predecessor_path: Path,
    predecessor_manifest_path: Path,
    predecessor_signature_path: Path,
    changed_services_path: Path,
    inherited_images_path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    env = _environment(environ)
    predecessor = json.loads(predecessor_path.read_text(encoding="utf-8"))
    changed = set(changed_services_path.read_text(encoding="utf-8").splitlines())
    if predecessor is None:
        if changed != set(SERVICES):
            raise SystemExit("a partial release requires a completed predecessor")
        inherited_images_path.write_text("", encoding="utf-8")
        return

    manifest = validate_manifest(predecessor.get("manifest"))
    validate_software_manifest(manifest)
    manifest_bytes = canonical_manifest_bytes(manifest)
    expected_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    if not (
        predecessor.get("status") == "completed"
        and predecessor.get("id") == manifest["release_id"]
        and predecessor.get("sequence") == manifest["sequence"]
        and predecessor.get("version") == manifest["version"]
        and predecessor.get("source_commit") == manifest["source_commit"]
        and predecessor.get("manifest_digest") == expected_digest
        and isinstance(predecessor.get("manifest_signature"), str)
        and predecessor.get("manifest_signature")
    ):
        raise SystemExit("predecessor SoftwareRelease identity is incomplete")
    predecessor_host = predecessor.get("paired_host_release")
    if not isinstance(predecessor_host, dict):
        raise SystemExit("predecessor HostRelease is missing")
    host_manifest = validate_host_manifest(predecessor_host.get("manifest"))
    if not (
        predecessor_host.get("status") == "completed"
        and predecessor_host.get("id") == host_manifest["release_id"]
        and predecessor_host.get("sequence") == host_manifest["sequence"]
        and predecessor_host.get("version") == host_manifest["version"]
        and predecessor_host.get("source_commit") == host_manifest["source_commit"]
        and predecessor_host.get("manifest_digest") == host_canonical_digest(host_manifest)
        and isinstance(predecessor_host.get("manifest_signature"), str)
        and predecessor_host.get("manifest_signature")
        and predecessor_host.get("software_release_id") == predecessor.get("id")
    ):
        raise SystemExit("predecessor HostRelease identity is incomplete")
    assert_pair(host_manifest, manifest)
    _decode_signature(predecessor_host, "HostRelease")
    if manifest["signing_key_sha256"] != env["SIGNING_KEY_SHA256"]:
        if changed != set(SERVICES):
            raise SystemExit("signing-key rotation requires rebuilding every service")
        inherited_images_path.write_text("", encoding="utf-8")
        return
    binding_changed = (
        manifest["host_release_id"] != env["HOST_RELEASE_ID"]
        or manifest["topology_digest"] != env["TOPOLOGY_DIGEST"]
    )
    if binding_changed and changed != set(SERVICES):
        raise SystemExit("new HostRelease binding requires rebuilding every service")
    try:
        signature = base64.b64decode(predecessor.get("manifest_signature", ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SystemExit("predecessor manifest signature is invalid") from exc
    if not signature:
        raise SystemExit("predecessor manifest signature is missing")
    predecessor_manifest_path.write_bytes(manifest_bytes)
    predecessor_signature_path.write_bytes(signature)
    inherited_images_path.write_text(
        "" if binding_changed else "".join(
            f"{service}\t{manifest['services'][service]}\t{manifest['image_ids'][service]}\n"
            for service in SERVICES
            if service not in changed
        ),
        encoding="utf-8",
    )


def resume_completed_release(
    resumable_path: Path,
    manifest_path: Path,
    signature_path: Path,
    images_path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env = _environment(environ)
    row = json.loads(resumable_path.read_text(encoding="utf-8"))
    if not isinstance(row, dict):
        raise SystemExit(1)
    manifest = validate_manifest(row.get("manifest"))
    validate_software_manifest(manifest)
    manifest_bytes = canonical_manifest_bytes(manifest)
    expected_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    if (
        row.get("status") != "completed"
        or row.get("id") != manifest["release_id"]
        or row.get("sequence") != manifest["sequence"]
        or row.get("version") != manifest["version"]
        or row.get("source_commit") != manifest["source_commit"]
        or row.get("manifest_digest") != expected_digest
        or row.get("version") != env["RELEASE_VERSION"]
        or row.get("source_commit") != env["RELEASE_SOURCE_COMMIT"]
        or manifest["host_release_id"] != env["HOST_RELEASE_ID"]
        or manifest["topology_digest"] != env["TOPOLOGY_DIGEST"]
        or manifest["registry"] != env["HARBOR_HOST"]
        or manifest["signing_key_sha256"] != env["SIGNING_KEY_SHA256"]
        or set(manifest["services"]) != set(SERVICES)
    ):
        raise SystemExit("resumable SoftwareRelease identity is invalid")
    for service in SERVICES:
        prefix = f"{env['HARBOR_HOST']}/{env['HARBOR_PROJECT']}/{service}@sha256:"
        if not manifest["services"][service].startswith(prefix):
            raise SystemExit("resumable SoftwareRelease repository is invalid")
    try:
        signature = base64.b64decode(row.get("manifest_signature", ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SystemExit("resumable SoftwareRelease signature is invalid") from exc
    if not signature:
        raise SystemExit("resumable SoftwareRelease signature is missing")
    manifest_path.write_bytes(manifest_bytes)
    signature_path.write_bytes(signature)
    images_path.write_text(
        "".join(
            f"{service}\t{manifest['services'][service]}\t{manifest['image_ids'][service]}\n"
            for service in SERVICES
        ),
        encoding="utf-8",
    )
    return row


def write_release_output(row_path: Path, *, environ: Mapping[str, str] | None = None) -> None:
    env = _environment(environ)
    row = json.loads(row_path.read_text(encoding="utf-8"))
    output = env.get("SOFTWARE_RELEASE_OUTPUT", "")
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(row, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        output_path.chmod(0o600)
    print(f"SoftwareRelease resumed: id={row['id']} digest={row['manifest_digest']}")


def create_manifest(
    image_map_path: Path,
    manifest_path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    env = _environment(environ)
    services: dict[str, str] = {}
    image_ids: dict[str, str] = {}
    for line in image_map_path.read_text(encoding="utf-8").splitlines():
        service, reference, image_id = line.split("\t")
        services[service] = reference
        image_ids[service] = image_id
    manifest = {
        "schema": "drycontroller.software-release.v1",
        "release_id": env["RELEASE_ID"],
        "host_release_id": env["HOST_RELEASE_ID"],
        "topology_digest": env["TOPOLOGY_DIGEST"],
        "sequence": int(env["RELEASE_SEQUENCE"]),
        "version": env["RELEASE_VERSION"],
        "source_commit": env["RELEASE_SOURCE_COMMIT"],
        "registry": env["HARBOR_HOST"],
        "signing_key_sha256": env["SIGNING_KEY_SHA256"],
        "cosign": COSIGN_TOOLCHAIN,
        "services": services,
        "image_ids": image_ids,
    }
    validate_manifest(manifest)
    manifest_path.write_bytes(canonical_manifest_bytes(manifest))


def complete_cloud_release(
    metadata_path: Path,
    manifest_path: Path,
    signature_path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    env = _environment(environ)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    signature = base64.b64encode(signature_path.read_bytes()).decode()
    body = json.dumps({"manifest": manifest, "manifest_signature": signature}, separators=(",", ":")).encode()
    request = urllib.request.Request(
        env["CLOUD_OPS_RELEASE_API_URL"].rstrip("/")
        + f"/api/v1/software-releases/{metadata['id']}/complete",
        data=body,
        headers={"Content-Type": "application/json", "X-API-Key": env["CLOUD_OPS_RELEASE_API_KEY"]},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read(1024 * 1024))
    expected_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    if payload.get("status") != "completed" or payload.get("manifest_digest") != expected_digest:
        raise SystemExit("Cloud did not complete the exact SoftwareRelease manifest")
    output = env.get("SOFTWARE_RELEASE_OUTPUT", "")
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        output_path.chmod(0o600)
    print(f"SoftwareRelease completed: id={metadata['id']} digest={expected_digest}")


def cloud_field(metadata_path: Path, field: str) -> str:
    value = json.loads(metadata_path.read_text(encoding="utf-8"))[field]
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("inventory")
    source_image = subparsers.add_parser("source-image")
    source_image.add_argument("service", choices=SOURCE_IMAGES)
    dockerfile = subparsers.add_parser("release-dockerfile")
    dockerfile.add_argument("service", choices=RELEASE_DOCKERFILES)
    docker_auth = subparsers.add_parser("write-docker-auth")
    docker_auth.add_argument("path", type=Path)
    predecessor = subparsers.add_parser("resolve-predecessor")
    predecessor.add_argument("predecessor", type=Path)
    predecessor.add_argument("resumable", type=Path)
    changed = subparsers.add_parser("resolve-changed-services")
    changed.add_argument("predecessor", type=Path)
    changed.add_argument("output", type=Path)
    create = subparsers.add_parser("create-cloud")
    create.add_argument("output", type=Path)
    portable = subparsers.add_parser("portable-image-id")
    portable.add_argument("archive", type=Path)
    inherited = subparsers.add_parser("load-inherited")
    for name in ("predecessor", "manifest", "signature", "changed", "images"):
        inherited.add_argument(name, type=Path)
    resume = subparsers.add_parser("resume")
    for name in ("row", "manifest", "signature", "images"):
        resume.add_argument(name, type=Path)
    output = subparsers.add_parser("write-release-output")
    output.add_argument("row", type=Path)
    manifest = subparsers.add_parser("create-manifest")
    manifest.add_argument("images", type=Path)
    manifest.add_argument("output", type=Path)
    complete = subparsers.add_parser("complete-cloud")
    complete.add_argument("metadata", type=Path)
    complete.add_argument("manifest", type=Path)
    complete.add_argument("signature", type=Path)
    field = subparsers.add_parser("field")
    field.add_argument("metadata", type=Path)
    field.add_argument("name")
    args = parser.parse_args()

    if args.command == "inventory":
        print("\n".join(SERVICES))
    elif args.command == "source-image":
        print(SOURCE_IMAGES[args.service])
    elif args.command == "release-dockerfile":
        print(RELEASE_DOCKERFILES[args.service])
    elif args.command == "write-docker-auth":
        write_docker_auth(args.path)
    elif args.command == "resolve-predecessor":
        resolve_predecessor(args.predecessor, args.resumable)
    elif args.command == "resolve-changed-services":
        resolve_changed_services(args.predecessor, args.output)
    elif args.command == "create-cloud":
        create_cloud_release(args.output)
    elif args.command == "portable-image-id":
        print(portable_image_id(args.archive))
    elif args.command == "load-inherited":
        load_inherited_images(args.predecessor, args.manifest, args.signature, args.changed, args.images)
    elif args.command == "resume":
        resume_completed_release(args.row, args.manifest, args.signature, args.images)
    elif args.command == "write-release-output":
        write_release_output(args.row)
    elif args.command == "create-manifest":
        create_manifest(args.images, args.output)
    elif args.command == "complete-cloud":
        complete_cloud_release(args.metadata, args.manifest, args.signature)
    elif args.command == "field":
        print(cloud_field(args.metadata, args.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
