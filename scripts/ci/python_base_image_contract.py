#!/usr/bin/env python3
"""Verify the immutable base identity of the three local Python service images."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any


PYTHON_SERVICES = (
    "edge-api",
    "edge-daq",
    "edge-monitor",
)
PYTHON_BASE_NAME = "docker.io/library/python:3.11-slim"
PYTHON_BASE_DIGEST = "sha256:272efc3f94bec901e2bc7a5c9984b3d113c317839edca2714c6a388b9476f8fc"
PYTHON_BASE_REFERENCE = f"python:3.11-slim@{PYTHON_BASE_DIGEST}"
PYTHON_BASE_PLATFORM = "linux/amd64"
BASE_NAME_LABEL = "org.opencontainers.image.base.name"
BASE_DIGEST_LABEL = "org.opencontainers.image.base.digest"
RECEIPT_SCHEMA = "drycontroller.python-base-image-receipt.v1"

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_FROM = re.compile(
    r"^[ \t]*FROM[ \t]+(?P<reference>\S+)(?:[ \t]+AS[ \t]+\S+)?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_MAX_JSON_BYTES = 4 * 1024 * 1024


class ContractError(ValueError):
    """The Dockerfile or built image does not match the approved identity."""


def _label_value(text: str, key: str) -> str | None:
    match = re.search(
        rf"^LABEL[ \t]+{re.escape(key)}=(?:\"([^\"]+)\"|'([^']+)'|(\S+))[ \t]*$",
        text,
        flags=re.MULTILINE,
    )
    if match is None:
        return None
    return next(value for value in match.groups() if value is not None)


def verify_dockerfile_text(text: str, service: str) -> dict[str, str]:
    references = [match.group("reference") for match in _FROM.finditer(text)]
    if len(references) != 1:
        raise ContractError(f"{service} Dockerfile must contain exactly one FROM instruction")
    reference = references[0]
    if "@sha256:" not in reference:
        raise ContractError(f"{service} Python base must be digest locked")
    if reference != PYTHON_BASE_REFERENCE:
        raise ContractError(f"{service} Dockerfile does not use the approved base reference")
    if _label_value(text, BASE_NAME_LABEL) != PYTHON_BASE_NAME:
        raise ContractError(f"{service} Dockerfile base name label mismatch")
    if _label_value(text, BASE_DIGEST_LABEL) != PYTHON_BASE_DIGEST:
        raise ContractError(f"{service} Dockerfile base digest label mismatch")
    return {
        "base_digest": PYTHON_BASE_DIGEST,
        "base_name": PYTHON_BASE_NAME,
        "base_reference": PYTHON_BASE_REFERENCE,
        "platform": PYTHON_BASE_PLATFORM,
        "service": service,
    }


def verify_dockerfiles(root: Path) -> dict[str, dict[str, str]]:
    records: dict[str, dict[str, str]] = {}
    for service in PYTHON_SERVICES:
        path = root / "services" / service / "Dockerfile"
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ContractError(f"cannot read {path}") from exc
        records[service] = verify_dockerfile_text(text, service)
    return records


def validate_image_inspect(
    service: str,
    image: object,
    *,
    expected_image_id: str | None = None,
) -> dict[str, str]:
    if service not in PYTHON_SERVICES:
        raise ContractError(f"unsupported Python service: {service}")
    if not isinstance(image, dict):
        raise ContractError(f"{service} Docker inspect payload invalid")
    if image.get("Os") != "linux" or image.get("Architecture") != "amd64":
        raise ContractError(f"{service} image platform must be {PYTHON_BASE_PLATFORM}")
    config = image.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict) or labels.get(BASE_NAME_LABEL) != PYTHON_BASE_NAME:
        raise ContractError(f"{service} image base name label mismatch")
    if labels.get(BASE_DIGEST_LABEL) != PYTHON_BASE_DIGEST:
        raise ContractError(f"{service} image base digest label mismatch")
    image_id = image.get("Id")
    if not isinstance(image_id, str) or _DIGEST.fullmatch(image_id) is None:
        raise ContractError(f"{service} image config digest invalid")
    if expected_image_id is not None and image_id != expected_image_id:
        raise ContractError(f"{service} image config digest does not match the manifest")
    return {
        "base_digest": PYTHON_BASE_DIGEST,
        "base_name": PYTHON_BASE_NAME,
        "image_id": image_id,
        "platform": PYTHON_BASE_PLATFORM,
        "service": service,
    }


def validate_image_archive(
    service: str,
    archive_path: Path,
    *,
    expected_image_id: str | None = None,
) -> dict[str, str]:
    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            manifest_file = archive.extractfile("manifest.json")
            if manifest_file is None:
                raise ContractError(f"{service} image archive manifest missing")
            manifest_bytes = manifest_file.read(_MAX_JSON_BYTES + 1)
            if len(manifest_bytes) > _MAX_JSON_BYTES:
                raise ContractError(f"{service} image archive manifest too large")
            manifest = json.loads(manifest_bytes)
            if not isinstance(manifest, list) or len(manifest) != 1 or not isinstance(manifest[0], dict):
                raise ContractError(f"{service} image archive must contain exactly one image")
            config_name = manifest[0].get("Config")
            if not isinstance(config_name, str) or not config_name:
                raise ContractError(f"{service} image archive config reference invalid")
            config_file = archive.extractfile(config_name)
            if config_file is None:
                raise ContractError(f"{service} image archive config missing")
            config_bytes = config_file.read(_MAX_JSON_BYTES + 1)
            if len(config_bytes) > _MAX_JSON_BYTES:
                raise ContractError(f"{service} image archive config too large")
            config = json.loads(config_bytes)
    except ContractError:
        raise
    except (OSError, tarfile.TarError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{service} image archive invalid") from exc
    if not isinstance(config, dict):
        raise ContractError(f"{service} image config invalid")
    config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    return validate_image_inspect(
        service,
        {
            "Architecture": config.get("architecture"),
            "Config": config.get("config"),
            "Id": config_digest,
            "Os": config.get("os"),
        },
        expected_image_id=expected_image_id,
    )


def inspect_image(
    service: str,
    reference: str,
    expected_image_id: str | None = None,
) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="drycontroller-python-base-") as directory:
        archive_path = Path(directory) / "image.tar"
        try:
            subprocess.run(
                ["docker", "image", "save", "-o", str(archive_path), reference],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ContractError(f"cannot save {service} image") from exc
        return validate_image_archive(
            service,
            archive_path,
            expected_image_id=expected_image_id,
        )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid JSON: {path}") from exc


def verify_receipts(receipt_dir: Path, manifest_path: Path) -> dict[str, object]:
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("software manifest is invalid") from exc
    image_ids = manifest.get("image_ids") if isinstance(manifest, dict) else None
    if not isinstance(image_ids, dict):
        raise ContractError("software manifest image_ids are invalid")
    expected_paths = {receipt_dir / f"{service}.json" for service in PYTHON_SERVICES}
    actual_paths = set(receipt_dir.glob("*.json"))
    if actual_paths != expected_paths:
        raise ContractError("Python base receipt set must cover exactly six services")

    proven_images: dict[str, str] = {}
    expected_fields = {"base_digest", "base_name", "image_id", "platform", "service"}
    for service in PYTHON_SERVICES:
        record = _read_json(receipt_dir / f"{service}.json")
        if not isinstance(record, dict) or set(record) != expected_fields:
            raise ContractError(f"{service} Python base receipt fields invalid")
        if (
            record.get("service") != service
            or record.get("base_name") != PYTHON_BASE_NAME
            or record.get("base_digest") != PYTHON_BASE_DIGEST
            or record.get("platform") != PYTHON_BASE_PLATFORM
            or record.get("image_id") != image_ids.get(service)
        ):
            raise ContractError(f"{service} Python base receipt does not match the software manifest")
        proven_images[service] = record["image_id"]

    return {
        "base": {
            "digest": PYTHON_BASE_DIGEST,
            "name": PYTHON_BASE_NAME,
            "platform": PYTHON_BASE_PLATFORM,
        },
        "images": proven_images,
        "schema": RECEIPT_SCHEMA,
        "software_manifest_digest": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    dockerfiles = subparsers.add_parser("verify-dockerfiles")
    dockerfiles.add_argument("--root", type=Path, default=Path("."))

    image = subparsers.add_parser("verify-image")
    image.add_argument("--service", choices=PYTHON_SERVICES, required=True)
    image.add_argument("--reference", required=True)
    image.add_argument("--expected-image-id")
    image.add_argument("--output", type=Path, required=True)

    receipts = subparsers.add_parser("verify-receipts")
    receipts.add_argument("--directory", type=Path, required=True)
    receipts.add_argument("--manifest", type=Path, required=True)
    receipts.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "verify-dockerfiles":
            payload = verify_dockerfiles(args.root)
            print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        elif args.command == "verify-image":
            _write_json(
                args.output,
                inspect_image(args.service, args.reference, args.expected_image_id),
            )
        else:
            _write_json(args.output, verify_receipts(args.directory, args.manifest))
    except ContractError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
