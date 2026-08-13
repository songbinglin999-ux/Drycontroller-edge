#!/usr/bin/env python3
"""Verify and package one completed SoftwareRelease for factory media."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER_DIR = ROOT / "infra" / "ota-helper"
if str(HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(HELPER_DIR))

from release_engine import (  # noqa: E402
    ReleaseError,
    assert_software_release_host_binding,
    canonical_manifest_bytes,
    canonical_manifest_digest,
    docker_archive_config_digest,
    local_image_reference,
    validate_host_active,
    validate_manifest,
)


MAX_SEED_BYTES = 64 * 1024
MAX_PUBLIC_KEY_BYTES = 64 * 1024


def _read_limited(path: Path, maximum: int, label: str) -> bytes:
    if not path.is_file() or path.stat().st_size > maximum:
        raise ValueError(f"{label} missing or too large")
    return path.read_bytes()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            os.fchmod(handle.fileno(), mode)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _durable_replace(temporary: Path, target: Path) -> None:
    descriptor = os.open(temporary, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, target)
    _fsync_directory(target.parent)


def _validate_linux_amd64_elf(binary: bytes) -> None:
    if (
        len(binary) < 20
        or binary[:4] != b"\x7fELF"
        or binary[4] != 2
        or binary[5] != 1
        or int.from_bytes(binary[18:20], "little") != 62
    ):
        raise ValueError("cosign target binary must be Linux amd64 ELF")


def _portable_image_id(reference: str, directory: Path) -> str:
    archive = directory / "image.tar"
    try:
        subprocess.run(["docker", "image", "save", "-o", str(archive), reference], check=True)
        return docker_archive_config_digest(archive)
    finally:
        archive.unlink(missing_ok=True)


def _normalize_release(payload: object, source_commit: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("software release payload must be an object")
    manifest = validate_manifest(payload.get("manifest"))
    if manifest["source_commit"] != source_commit:
        raise ValueError("software release source commit does not match installer source")
    digest = canonical_manifest_digest(manifest)
    if payload.get("manifest_digest") != digest:
        raise ValueError("software release manifest digest mismatch")
    signature = payload.get("manifest_signature")
    if not isinstance(signature, str) or not signature:
        raise ValueError("software release manifest signature missing")
    try:
        signature_bytes = base64.b64decode(signature, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("software release manifest signature is not base64") from exc
    if not signature_bytes or len(signature_bytes) > 16 * 1024:
        raise ValueError("software release manifest signature size invalid")

    if payload.get("status") != "completed":
        raise ValueError("software release is not completed")
    for outer, inner in (
        ("id", "release_id"),
        ("sequence", "sequence"),
        ("version", "version"),
        ("source_commit", "source_commit"),
    ):
        if outer in payload and payload.get(outer) != manifest[inner]:
            raise ValueError(f"software release {outer} does not match manifest")
    return {
        "manifest": manifest,
        "manifest_digest": digest,
        "manifest_signature": signature,
    }


def prepare(
    *,
    software_release: Path,
    public_key: Path,
    cosign_linux_amd64: Path,
    source_commit: str,
    host_active_file: Path,
    output_dir: Path,
    image_bundle: Path,
) -> dict[str, object]:
    if shutil.which("docker") is None or shutil.which("cosign") is None:
        raise ValueError("docker and cosign are required on the media builder")
    payload = json.loads(_read_limited(software_release, MAX_SEED_BYTES, "software release").decode("utf-8"))
    seed = _normalize_release(payload, source_commit)
    manifest = seed["manifest"]
    assert isinstance(manifest, dict)
    host_active_bytes = _read_limited(host_active_file, MAX_SEED_BYTES, "active HostRelease state")
    host_active = validate_host_active(
        json.loads(host_active_bytes.decode("utf-8")),
        (ROOT / "docker-compose.yml").read_bytes(),
    )
    assert_software_release_host_binding(manifest, host_active)

    public_key_bytes = _read_limited(public_key, MAX_PUBLIC_KEY_BYTES, "cosign public key")
    actual_key_digest = "sha256:" + hashlib.sha256(public_key_bytes).hexdigest()
    if manifest["signing_key_sha256"] != actual_key_digest:
        raise ValueError("cosign public key does not match SoftwareRelease")
    target_cosign = _read_limited(cosign_linux_amd64, 256 * 1024 * 1024, "cosign target binary")
    _validate_linux_amd64_elf(target_cosign)
    actual_cosign_digest = "sha256:" + hashlib.sha256(target_cosign).hexdigest()
    if manifest["cosign"]["linux_amd64_sha256"] != actual_cosign_digest:
        raise ValueError("Linux amd64 cosign binary does not match SoftwareRelease")

    signature_bytes = base64.b64decode(str(seed["manifest_signature"]), validate=True)
    with tempfile.TemporaryDirectory(prefix="drycontroller-initial-release-") as temporary:
        temporary_dir = Path(temporary)
        manifest_path = temporary_dir / "manifest.json"
        signature_path = temporary_dir / "manifest.sigstore.json"
        manifest_path.write_bytes(canonical_manifest_bytes(manifest))
        signature_path.write_bytes(signature_bytes)
        subprocess.run(
            [
                "cosign",
                "verify-blob",
                "--key",
                str(public_key),
                "--insecure-ignore-tlog=true",
                "--bundle",
                str(signature_path),
                str(manifest_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    services = sorted(manifest["services"])
    references = [
        local_image_reference(service, manifest["image_ids"][service]) for service in services
    ]
    with tempfile.TemporaryDirectory(prefix="drycontroller-image-id-") as temporary:
        image_id_dir = Path(temporary)
        for service in services:
            reference = manifest["services"][service]
            subprocess.run(["docker", "pull", reference], check=True)
            subprocess.run(
                ["cosign", "verify", "--insecure-ignore-tlog=true", "--key", str(public_key), reference],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            expected_image_id = manifest["image_ids"][service]
            if _portable_image_id(reference, image_id_dir) != expected_image_id:
                raise ValueError(f"pulled image ID does not match signed manifest: {reference}")
            subprocess.run(
                ["docker", "tag", reference, local_image_reference(service, expected_image_id)],
                check=True,
            )

    image_bundle.parent.mkdir(parents=True, exist_ok=True)
    temporary_bundle = image_bundle.with_name(f".{image_bundle.name}.tmp")
    try:
        subprocess.run(["docker", "image", "save", "-o", str(temporary_bundle), *references], check=True)
        if not temporary_bundle.is_file() or temporary_bundle.stat().st_size == 0:
            raise ValueError("Docker produced an empty initial image bundle")
        _durable_replace(temporary_bundle, image_bundle)
    finally:
        temporary_bundle.unlink(missing_ok=True)

    seed_bytes = json.dumps(seed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _atomic_write(output_dir / "software-release-seed.json", seed_bytes, 0o600)
    _atomic_write(output_dir / "host-active.json", host_active_bytes, 0o600)
    _atomic_write(output_dir / "cosign.pub", public_key_bytes, 0o644)
    _atomic_write(output_dir / "cosign-linux-amd64", target_cosign, 0o755)
    return seed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--software-release", required=True, type=Path)
    parser.add_argument("--cosign-public-key", required=True, type=Path)
    parser.add_argument("--cosign-linux-amd64", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--host-active", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--image-bundle", required=True, type=Path)
    args = parser.parse_args()
    try:
        seed = prepare(
            software_release=args.software_release,
            public_key=args.cosign_public_key,
            cosign_linux_amd64=args.cosign_linux_amd64,
            source_commit=args.source_commit,
            host_active_file=args.host_active,
            output_dir=args.output_dir,
            image_bundle=args.image_bundle,
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        ReleaseError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"initial release preparation failed: {exc}", file=sys.stderr)
        return 1
    manifest = seed["manifest"]
    assert isinstance(manifest, dict)
    print(
        f"initial release prepared: id={manifest['release_id']} digest={seed['manifest_digest']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
