#!/usr/bin/python3
"""RB-6 ISO remastering and final public-material verification helpers."""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parents[1]
AUTOINSTALL_DIR = REPO_ROOT / "dist" / "autoinstall"
RUNNER_DIR = REPO_ROOT / "infra" / "host-release-runner"
BOOTSTRAP_DIR = REPO_ROOT / "infra" / "bootstrap"
if str(RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(RUNNER_DIR))
if str(BOOTSTRAP_DIR) not in sys.path:
    sys.path.insert(0, str(BOOTSTRAP_DIR))

from host_release_engine import (  # noqa: E402
    COSIGN_TOOLCHAIN,
    HostReleaseEngine,
    HostReleaseError,
    _docker_archive_digests,
    _local_image_reference,
    assert_pair,
    canonical_digest,
    canonical_json_bytes,
    validate_host_manifest,
    validate_software_manifest,
)
from installer_public_config import validate_public_config  # noqa: E402
from tailscale_client_lock import validate_tailscale_client_lock  # noqa: E402


MEDIA_MAPS = (
    ("dist/autoinstall/user-data", "/user-data"),
    ("dist/autoinstall/meta-data", "/meta-data"),
    ("dist/autoinstall/post-install.sh", "/post-install.sh"),
    (
        "dist/autoinstall/drycontroller-edge-post-install.service",
        "/drycontroller-edge-post-install.service",
    ),
    ("dist/autoinstall/drycontroller-edge.tar.gz", "/drycontroller-edge.tar.gz"),
    ("dist/autoinstall/drycontroller-apt", "/drycontroller-apt"),
    ("dist/autoinstall/identity-wheelhouse", "/identity-wheelhouse"),
    ("dist/autoinstall/host-release-seed.json", "/host-release-seed.json"),
    ("dist/autoinstall/software-release-seed.json", "/software-release-seed.json"),
    ("dist/host-image.tar", "/host-image.tar"),
    ("dist/software-images.tar", "/software-images.tar"),
    ("dist/autoinstall/host-release-cosign.pub", "/host-release-cosign.pub"),
    ("dist/autoinstall/software-release-cosign.pub", "/software-release-cosign.pub"),
    ("dist/autoinstall/cosign-linux-amd64", "/cosign-linux-amd64"),
    ("dist/autoinstall/device-identity-cloud.pub", "/device-identity-cloud.pub"),
    ("dist/autoinstall/public-config.json", "/public-config.json"),
    ("dist/autoinstall/tailscale-client.json", "/tailscale-client.json"),
)


def _log(app: Any, message: str, level: str = "info") -> None:
    append = getattr(app, "_append_log", None)
    if callable(append):
        append(message, level)


def _run(app: Any, args: list[str]) -> None:
    _log(app, "$ " + " ".join(args))
    subprocess.run(args, cwd=REPO_ROOT, check=True)


def build_iso(app: Any, plan: dict[str, object]) -> None:
    if os.environ.get("DRYCONTROLLER_RELEASE_WRAPPER") != "1":
        raise ValueError("ISO remastering must run through build-os-installer-release.sh")
    if plan.get("include_images") is not True:
        raise ValueError("formal OS Installer media must include the complete signed image pair")
    base_iso = Path(str(plan["base_iso"])).resolve()
    output_iso = Path(str(plan["output_iso"])).resolve()
    if base_iso == output_iso:
        raise ValueError("output ISO must differ from the base ISO")
    if not base_iso.is_file():
        raise ValueError("base ISO missing")
    if shutil.which("xorriso") is None:
        raise ValueError("xorriso is required on the fixed Linux amd64 builder")
    for source, _target in MEDIA_MAPS:
        if not (REPO_ROOT / source).exists():
            raise ValueError(f"release media input missing: {source}")
    output_iso.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "xorriso",
        "-indev",
        str(base_iso),
        "-outdev",
        str(output_iso),
        "-boot_image",
        "any",
        "replay",
        "-overwrite",
        "on",
    ]
    for source, target in MEDIA_MAPS:
        args.extend(["-map", source, target])
    _run(app, args)
    verify_final_iso(app, output_iso, include_images=True)


def extract_iso_file(iso: Path, path: str) -> bytes:
    try:
        return subprocess.check_output(
            ["bsdtar", "-xOf", str(iso), path],
            cwd=REPO_ROOT,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ISO missing required file: {path}") from exc


def _seed(raw: bytes, *, host: bool) -> tuple[dict[str, Any], str, str]:
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {
        "manifest",
        "manifest_digest",
        "manifest_signature",
    }:
        raise RuntimeError("ISO initial release seed fields invalid")
    manifest = (
        validate_host_manifest(value["manifest"])
        if host
        else validate_software_manifest(value["manifest"])
    )
    digest = canonical_digest(manifest)
    if value["manifest_digest"] != digest:
        raise RuntimeError("ISO initial release seed digest mismatch")
    signature = value["manifest_signature"]
    try:
        decoded = base64.b64decode(signature, validate=True)
    except (TypeError, binascii.Error, ValueError) as exc:
        raise RuntimeError("ISO initial release signature invalid") from exc
    if not decoded or len(decoded) > 16 * 1024:
        raise RuntimeError("ISO initial release signature invalid")
    return manifest, digest, signature


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _verify_seed_signature(
    *,
    directory: Path,
    prefix: str,
    manifest: dict[str, Any],
    signature: str,
    public_key: bytes,
    cosign: Path,
) -> None:
    manifest_path = directory / f"{prefix}-manifest.json"
    signature_path = directory / f"{prefix}-manifest.sigstore.json"
    public_key_path = directory / f"{prefix}-cosign.pub"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    signature_path.write_bytes(base64.b64decode(signature, validate=True))
    public_key_path.write_bytes(public_key)
    try:
        completed = subprocess.run(
            [
                str(cosign),
                "verify-blob",
                "--key",
                str(public_key_path),
                "--insecure-ignore-tlog=true",
                "--bundle",
                str(signature_path),
                str(manifest_path),
            ],
            cwd=directory,
            env={"HOME": str(directory), "PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"ISO {prefix} release signature verifier unavailable") from exc
    if completed.returncode != 0:
        raise RuntimeError(f"ISO {prefix} release signature invalid")


def _iso_entries(iso: Path) -> set[str]:
    raw = subprocess.check_output(["bsdtar", "-tf", str(iso)], cwd=REPO_ROOT, text=True)
    return {line.strip().removeprefix("./").rstrip("/") for line in raw.splitlines() if line.strip()}


def _verify_wheelhouse(iso: Path, entries: set[str]) -> None:
    wheel_entries = sorted(
        entry
        for entry in entries
        if entry.startswith("identity-wheelhouse/") and entry.endswith(".whl")
    )
    if not wheel_entries:
        raise RuntimeError("ISO identity wheelhouse is empty")
    sums = extract_iso_file(iso, "identity-wheelhouse/SHA256SUMS").decode("ascii").splitlines()
    expected: dict[str, str] = {}
    for line in sums:
        match = re.fullmatch(r"([0-9a-f]{64})  (.+\.whl)", line)
        if match is None:
            raise RuntimeError("ISO identity wheelhouse checksum manifest invalid")
        expected[f"identity-wheelhouse/{match.group(2).removeprefix('./')}"] = match.group(1)
    if set(expected) != set(wheel_entries):
        raise RuntimeError("ISO identity wheelhouse checksum set mismatch")
    for entry, digest in expected.items():
        if _sha256(extract_iso_file(iso, entry)) != digest:
            raise RuntimeError("ISO identity wheel checksum mismatch")


def _select_signature_verifier(
    *,
    directory: Path,
    embedded_linux_cosign: bytes,
    host_verifier: Path | None,
) -> Path:
    if host_verifier is not None:
        verifier = host_verifier.resolve()
        if not verifier.is_file() or not os.access(verifier, os.X_OK):
            raise RuntimeError("host cosign signature verifier unavailable")
        return verifier
    verifier = directory / "cosign-linux-amd64"
    verifier.write_bytes(embedded_linux_cosign)
    verifier.chmod(0o700)
    return verifier


def verify_final_iso(
    app: Any,
    iso: Path,
    *,
    include_images: bool,
    signature_verifier: Path | None = None,
) -> None:
    if include_images is not True:
        raise RuntimeError("formal ISO verification requires the complete signed image pair")
    _log(app, "Verifying final RB-6 ISO checklist")
    entries = _iso_entries(iso)
    required = {target.removeprefix("/") for _source, target in MEDIA_MAPS}
    required.update({"drycontroller-apt/Packages", "drycontroller-apt/Packages.gz"})
    missing = sorted(required - entries)
    if missing:
        raise RuntimeError("ISO missing required public release files: " + ", ".join(missing))
    forbidden_names = {
        "activation-bundle.json",
        "tailnet.env",
        "factory-license.json",
        "wifi.env",
        "password.env",
        "identity-key.pem",
    }
    forbidden = sorted(
        entry
        for entry in entries
        if Path(entry).name in forbidden_names or entry.endswith((".key", ".pem.private"))
    )
    if forbidden:
        raise RuntimeError("ISO contains secret or retired factory material: " + ", ".join(forbidden))

    user_data = extract_iso_file(iso, "user-data").decode("utf-8")
    for required_text in ('password: "!"', "allow-pw: false", "/cdrom/host-release-seed.json"):
        if required_text not in user_data:
            raise RuntimeError(f"ISO user-data missing fail-closed marker: {required_text}")
    for forbidden_text in ("REPLACE_WITH_SHA512_CRYPT_HASH", "allow-pw: true", "wifi.env"):
        if forbidden_text in user_data:
            raise RuntimeError(f"ISO user-data contains credential path: {forbidden_text}")

    try:
        validate_public_config(json.loads(extract_iso_file(iso, "public-config.json")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("ISO public configuration invalid") from exc

    try:
        tailscale_client = validate_tailscale_client_lock(
            json.loads(extract_iso_file(iso, "tailscale-client.json"))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("ISO Tailscale client lock invalid") from exc
    deb_name = tailscale_client["deb_url"].rsplit("/", 1)[-1]
    deb_entry = f"drycontroller-apt/pool/{deb_name}"
    if deb_entry not in entries:
        raise RuntimeError("ISO pinned Tailscale client DEB missing")
    if _sha256(extract_iso_file(iso, deb_entry)) != tailscale_client["deb_sha256"]:
        raise RuntimeError("ISO pinned Tailscale client DEB checksum mismatch")
    package_stanzas = extract_iso_file(iso, "drycontroller-apt/Packages").decode(
        "utf-8"
    ).split("\n\n")
    expected_package_fields = {
        "Package": "tailscale",
        "Version": tailscale_client["version"],
        "Architecture": tailscale_client["architecture"],
        "Filename": f"pool/{deb_name}",
        "SHA256": tailscale_client["deb_sha256"],
    }
    package_match = False
    for stanza in package_stanzas:
        fields = {
            name: value
            for line in stanza.splitlines()
            if ": " in line
            for name, value in [line.split(": ", 1)]
        }
        if all(fields.get(name) == value for name, value in expected_package_fields.items()):
            package_match = True
            break
    if not package_match:
        raise RuntimeError("ISO apt metadata does not bind the pinned Tailscale client")

    host, host_digest, _host_signature = _seed(
        extract_iso_file(iso, "host-release-seed.json"), host=True
    )
    software, software_digest, _software_signature = _seed(
        extract_iso_file(iso, "software-release-seed.json"), host=False
    )
    assert_pair(host, software)
    host_key = extract_iso_file(iso, "host-release-cosign.pub")
    software_key = extract_iso_file(iso, "software-release-cosign.pub")
    if host_key == software_key:
        raise RuntimeError("ISO HostRelease and SoftwareRelease trust roots must differ")
    if "sha256:" + _sha256(host_key) != host["signing_key_sha256"]:
        raise RuntimeError("ISO HostRelease trust root mismatch")
    if "sha256:" + _sha256(software_key) != software["signing_key_sha256"]:
        raise RuntimeError("ISO SoftwareRelease trust root mismatch")
    cosign = extract_iso_file(iso, "cosign-linux-amd64")
    if "sha256:" + _sha256(cosign) != COSIGN_TOOLCHAIN["linux_amd64_sha256"]:
        raise RuntimeError("ISO cosign binary checksum mismatch")
    identity_key = base64.b64decode(
        extract_iso_file(iso, "device-identity-cloud.pub").decode("ascii").strip(),
        validate=True,
    )
    if len(identity_key) != 32:
        raise RuntimeError("ISO Cloud identity public key invalid")

    with tempfile.TemporaryDirectory(prefix="drycontroller-iso-verify-") as temporary:
        directory = Path(temporary)
        cosign_path = _select_signature_verifier(
            directory=directory,
            embedded_linux_cosign=cosign,
            host_verifier=signature_verifier,
        )
        _verify_seed_signature(
            directory=directory,
            prefix="host",
            manifest=host,
            signature=_host_signature,
            public_key=host_key,
            cosign=cosign_path,
        )
        _verify_seed_signature(
            directory=directory,
            prefix="software",
            manifest=software,
            signature=_software_signature,
            public_key=software_key,
            cosign=cosign_path,
        )
        host_archive = directory / "host-image.tar"
        software_archive = directory / "software-images.tar"
        host_archive.write_bytes(extract_iso_file(iso, "host-image.tar"))
        software_archive.write_bytes(extract_iso_file(iso, "software-images.tar"))
        staging = directory / "host-staging"
        staging.mkdir()
        try:
            HostReleaseEngine()._extract_bundle(host_archive, host, staging)
        except HostReleaseError as exc:
            raise RuntimeError("ISO HostRelease archive verification failed") from exc
        expected_images = {
            _local_image_reference(service, software["image_ids"][service]): software["image_ids"][service]
            for service in sorted(software["services"])
        }
        if _docker_archive_digests(software_archive) != expected_images:
            raise RuntimeError("ISO SoftwareRelease image archive mismatch")

    _verify_wheelhouse(iso, entries)
    grub = extract_iso_file(iso, "boot/grub/grub.cfg").decode("utf-8", errors="replace")
    if "DryController Edge Autoinstall" not in grub or "autoinstall ds=nocloud\\;s=/cdrom/" not in grub:
        raise RuntimeError("ISO GRUB autoinstall entry invalid")
    package_index = next(
        (
            entry
            for entry in sorted(entries)
            if re.fullmatch(r"dists/[^/]+/main/binary-amd64/Packages\.gz", entry)
        ),
        None,
    )
    if package_index is None:
        raise RuntimeError("ISO Ubuntu amd64 package index missing")
    packages = "\n" + gzip.decompress(extract_iso_file(iso, package_index)).decode(
        "utf-8", errors="replace"
    )
    missing_boot = [
        package
        for package in ("linux-generic", "linux-image-generic", "grub-pc", "grub-efi-amd64")
        if f"\nPackage: {package}\n" not in packages
    ]
    if missing_boot:
        raise RuntimeError("ISO boot package set incomplete: " + ", ".join(missing_boot))
    _log(
        app,
        f"Final ISO checklist passed: H={host_digest} S={software_digest}",
    )
