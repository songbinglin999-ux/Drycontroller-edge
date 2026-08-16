#!/usr/bin/env python3
"""Build, verify, and publish the public H1/T1/S1 release handoff."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER_DIR = ROOT / "infra" / "host-release-runner"
if str(RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(RUNNER_DIR))

from host_release_engine import (  # noqa: E402
    assert_pair,
    canonical_digest,
    canonical_json_bytes,
    validate_host_manifest,
    validate_software_manifest,
)


SCHEMA = "drycontroller.release-handoff.v1"
KEY_KIND = "release-public-keys"
PAIR_KIND = "release-pair"
APPROVED_GITEA_BASE_URL = "https://git.example.com"
GITEA_OWNER = "example-org"
GITEA_REPOSITORY = "Drycontroller-edge"
KEY_PAYLOAD = (
    "host-release-cosign.pub",
    "software-release-cosign.pub",
)
PAIR_PAYLOAD = (
    "host-manifest.json",
    "host-manifest.sigstore.json",
    "host-release-cosign.pub",
    "host-release.json",
    "software-manifest.json",
    "software-manifest.sigstore.json",
    "software-release-cosign.pub",
    "software-release.json",
)
BOOKKEEPING = ("SHA256SUMS", "inventory.json")
MAX_RELEASE_BYTES = 4 * 1024 * 1024
MAX_KEY_BYTES = 64 * 1024
MAX_SIGNATURE_BYTES = 64 * 1024
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_TAG_RE = re.compile(
    r"^drycontroller-(?:release-public-keys|release-pair)-sha256-[0-9a-f]{64}$"
)
RELEASE_ARCHIVE = "release-handoff.tar"
RELEASE_CHECKSUM = "release-handoff.tar.sha256"
RELEASE_ASSETS = (RELEASE_ARCHIVE, RELEASE_CHECKSUM, "inventory.json")


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # noqa: N802
        return None


def _canonical_json(data: object) -> bytes:
    return json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _read_limited(path: Path, maximum: int, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be one regular file")
    size = path.stat().st_size
    if size < 1 or size > maximum:
        raise ValueError(f"{label} size is invalid")
    return path.read_bytes()


def _load_json(path: Path, maximum: int, label: str) -> object:
    try:
        return json.loads(_read_limited(path, maximum, label))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc


def _release_seed(
    path: Path, *, host: bool
) -> tuple[dict[str, object], dict[str, object], bytes]:
    label = "HostRelease" if host else "SoftwareRelease"
    payload = _load_json(path, MAX_RELEASE_BYTES, label)
    if not isinstance(payload, dict):
        raise ValueError(f"{label} payload must be an object")
    manifest = (
        validate_host_manifest(payload.get("manifest"))
        if host
        else validate_software_manifest(payload.get("manifest"))
    )
    digest = canonical_digest(manifest)
    if payload.get("status") != "completed" or payload.get("manifest_digest") != digest:
        raise ValueError(f"{label} is not one digest-bound completed release")
    for outer, inner in (
        ("id", "release_id"),
        ("sequence", "sequence"),
        ("version", "version"),
        ("source_commit", "source_commit"),
    ):
        if payload.get(outer) != manifest[inner]:
            raise ValueError(f"{label} {outer} does not match its manifest")
    signature = payload.get("manifest_signature")
    try:
        signature_bytes = base64.b64decode(signature, validate=True)
    except (binascii.Error, TypeError, ValueError) as exc:
        raise ValueError(f"{label} signature bundle is invalid") from exc
    if not signature_bytes or len(signature_bytes) > MAX_SIGNATURE_BYTES:
        raise ValueError(f"{label} signature bundle size is invalid")
    return payload, manifest, signature_bytes


def _public_key(path: Path, label: str) -> bytes:
    raw = _read_limited(path, MAX_KEY_BYTES, label)
    if not (
        raw.startswith(b"-----BEGIN PUBLIC KEY-----\n")
        and raw.rstrip().endswith(b"-----END PUBLIC KEY-----")
    ):
        raise ValueError(f"{label} is not a PEM public key")
    return raw


def _verify_signature(
    *,
    manifest: dict[str, object],
    signature: bytes,
    public_key: Path,
    cosign: Path,
    label: str,
) -> None:
    if not cosign.is_file():
        raise ValueError("approved cosign executable is unavailable")
    with tempfile.TemporaryDirectory(
        prefix="drycontroller-handoff-signature-"
    ) as temporary:
        root = Path(temporary)
        manifest_path = root / "manifest.json"
        signature_path = root / "manifest.sigstore.json"
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        signature_path.write_bytes(signature)
        completed = subprocess.run(
            [
                str(cosign),
                "verify-blob",
                "--key",
                str(public_key),
                "--insecure-ignore-tlog=true",
                "--bundle",
                str(signature_path),
                str(manifest_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    if completed.returncode != 0:
        raise ValueError(f"{label} signature verification failed")


def _expected_files(kind: str) -> tuple[str, ...]:
    payload = (
        KEY_PAYLOAD if kind == KEY_KIND else PAIR_PAYLOAD if kind == PAIR_KIND else None
    )
    if payload is None:
        raise ValueError("release handoff kind is invalid")
    return tuple(sorted((*payload, *BOOKKEEPING)))


def _payload_files(kind: str) -> tuple[str, ...]:
    if kind == KEY_KIND:
        return KEY_PAYLOAD
    if kind == PAIR_KIND:
        return PAIR_PAYLOAD
    raise ValueError("release handoff kind is invalid")


def _ensure_empty_output(output_dir: Path) -> None:
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise ValueError("handoff output must be one existing directory")
    if any(output_dir.iterdir()):
        raise ValueError("handoff output directory must be empty")


def _write_file(path: Path, raw: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _inventory_files(root: Path, names: tuple[str, ...]) -> list[dict[str, object]]:
    records = []
    for name in sorted(names):
        raw = _read_limited(root / name, MAX_RELEASE_BYTES, name)
        records.append({"name": name, "sha256": _sha256(raw), "size": len(raw)})
    return records


def _finish_package(root: Path, inventory: dict[str, object]) -> None:
    inventory["files"] = _inventory_files(
        root, tuple(item["name"] for item in inventory["files"])
    )
    _write_file(root / "inventory.json", _canonical_json(inventory) + b"\n")
    checksum_names = sorted(
        (*[item["name"] for item in inventory["files"]], "inventory.json")
    )
    checksums = b"".join(
        f"{hashlib.sha256((root / name).read_bytes()).hexdigest()}  {name}\n".encode()
        for name in checksum_names
    )
    _write_file(root / "SHA256SUMS", checksums)


def _scan_public_package(root: Path, kind: str) -> None:
    if tuple(sorted(path.name for path in root.iterdir())) != _expected_files(kind):
        raise ValueError("release handoff contains a non-allowlisted file")
    with tempfile.TemporaryDirectory(prefix="drycontroller-handoff-scan-") as temporary:
        temporary_root = Path(temporary)
        archive_path = temporary_root / "release-handoff.tar"
        report_path = temporary_root / "public-material-report.json"
        with tarfile.open(
            archive_path, mode="w", format=tarfile.USTAR_FORMAT
        ) as archive:
            for name in _expected_files(kind):
                raw = (root / name).read_bytes()
                info = tarfile.TarInfo(name)
                info.size = len(raw)
                info.mode = 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                archive.addfile(info, io.BytesIO(raw))
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "public-material-scan.py"),
                "--surfaces",
                "source",
                "--source",
                str(archive_path),
                "--report",
                str(report_path),
                "--timeout-seconds",
                "60",
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError("release handoff failed public-material scan")


def _install_staged(staging: Path, output_dir: Path, kind: str) -> None:
    _scan_public_package(staging, kind)
    for name in _expected_files(kind):
        os.replace(staging / name, output_dir / name)


def prepare_keys(
    *,
    host_public_key: Path,
    software_public_key: Path,
    source_commit: str,
    output_dir: Path,
) -> None:
    _ensure_empty_output(output_dir)
    if not COMMIT_RE.fullmatch(source_commit):
        raise ValueError("source commit must be one lowercase 40-hex commit")
    host_key = _public_key(host_public_key, "HostRelease public key")
    software_key = _public_key(software_public_key, "SoftwareRelease public key")
    if host_key == software_key:
        raise ValueError("HostRelease and SoftwareRelease trust roots must differ")
    with tempfile.TemporaryDirectory(prefix="drycontroller-key-handoff-") as temporary:
        staging = Path(temporary)
        _write_file(staging / "host-release-cosign.pub", host_key)
        _write_file(staging / "software-release-cosign.pub", software_key)
        inventory = {
            "schema": SCHEMA,
            "kind": KEY_KIND,
            "source_commit": source_commit,
            "host_signing_key_sha256": _sha256(host_key),
            "software_signing_key_sha256": _sha256(software_key),
            "files": [{"name": name} for name in KEY_PAYLOAD],
        }
        _finish_package(staging, inventory)
        _install_staged(staging, output_dir, KEY_KIND)


def prepare_pair(
    *,
    host_release: Path,
    software_release: Path,
    host_public_key: Path,
    software_public_key: Path,
    cosign: Path,
    output_dir: Path,
) -> None:
    _ensure_empty_output(output_dir)
    host_payload, host_manifest, host_signature = _release_seed(host_release, host=True)
    software_payload, software_manifest, software_signature = _release_seed(
        software_release, host=False
    )
    assert_pair(host_manifest, software_manifest)
    host_key = _public_key(host_public_key, "HostRelease public key")
    software_key = _public_key(software_public_key, "SoftwareRelease public key")
    if host_key == software_key:
        raise ValueError("HostRelease and SoftwareRelease trust roots must differ")
    if host_manifest["signing_key_sha256"] != _sha256(host_key):
        raise ValueError("HostRelease public key digest does not match its manifest")
    if software_manifest["signing_key_sha256"] != _sha256(software_key):
        raise ValueError(
            "SoftwareRelease public key digest does not match its manifest"
        )
    _verify_signature(
        manifest=host_manifest,
        signature=host_signature,
        public_key=host_public_key,
        cosign=cosign,
        label="HostRelease",
    )
    _verify_signature(
        manifest=software_manifest,
        signature=software_signature,
        public_key=software_public_key,
        cosign=cosign,
        label="SoftwareRelease",
    )
    with tempfile.TemporaryDirectory(prefix="drycontroller-pair-handoff-") as temporary:
        staging = Path(temporary)
        content = {
            "host-release.json": _canonical_json(host_payload) + b"\n",
            "software-release.json": _canonical_json(software_payload) + b"\n",
            "host-release-cosign.pub": host_key,
            "software-release-cosign.pub": software_key,
            "host-manifest.json": canonical_json_bytes(host_manifest),
            "software-manifest.json": canonical_json_bytes(software_manifest),
            "host-manifest.sigstore.json": host_signature,
            "software-manifest.sigstore.json": software_signature,
        }
        for name, raw in content.items():
            _write_file(staging / name, raw)
        inventory = {
            "schema": SCHEMA,
            "kind": PAIR_KIND,
            "source_commit": host_manifest["source_commit"],
            "topology_digest": host_manifest["topology_digest"],
            "host_release": {
                "id": host_manifest["release_id"],
                "version": host_manifest["version"],
                "manifest_digest": canonical_digest(host_manifest),
                "signing_key_sha256": host_manifest["signing_key_sha256"],
            },
            "software_release": {
                "id": software_manifest["release_id"],
                "version": software_manifest["version"],
                "manifest_digest": canonical_digest(software_manifest),
                "signing_key_sha256": software_manifest["signing_key_sha256"],
            },
            "files": [{"name": name} for name in PAIR_PAYLOAD],
        }
        _finish_package(staging, inventory)
        _install_staged(staging, output_dir, PAIR_KIND)


def _verify_checksums(root: Path, expected: tuple[str, ...]) -> None:
    raw = _read_limited(root / "SHA256SUMS", MAX_RELEASE_BYTES, "SHA256SUMS")
    lines = raw.decode("utf-8").splitlines()
    expected_names = sorted(name for name in expected if name != "SHA256SUMS")
    if len(lines) != len(expected_names):
        raise ValueError("SHA256SUMS inventory is incomplete")
    actual_names = []
    for line in lines:
        if re.fullmatch(r"[0-9a-f]{64}  [0-9A-Za-z._+\-]+", line) is None:
            raise ValueError("SHA256SUMS line is invalid")
        digest, name = line.split("  ", 1)
        actual_names.append(name)
        if (
            name not in expected_names
            or hashlib.sha256((root / name).read_bytes()).hexdigest() != digest
        ):
            raise ValueError("SHA256SUMS digest mismatch")
    if actual_names != expected_names:
        raise ValueError("SHA256SUMS names are not canonical")


def verify_package(root: Path, *, cosign: Path | None = None) -> str:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("release handoff must be one directory")
    inventory = _load_json(
        root / "inventory.json", MAX_RELEASE_BYTES, "handoff inventory"
    )
    if not isinstance(inventory, dict) or inventory.get("schema") != SCHEMA:
        raise ValueError("release handoff inventory schema is invalid")
    kind = str(inventory.get("kind") or "")
    expected = _expected_files(kind)
    if tuple(sorted(path.name for path in root.iterdir())) != expected:
        raise ValueError("release handoff contains a non-allowlisted file")
    records = inventory.get("files")
    if not isinstance(records, list) or len(records) != len(_payload_files(kind)):
        raise ValueError("release handoff file inventory is invalid")
    expected_records = _inventory_files(root, _payload_files(kind))
    if records != expected_records:
        raise ValueError("release handoff file inventory does not match payload")
    _verify_checksums(root, expected)
    if kind == KEY_KIND:
        host_key = _public_key(
            root / "host-release-cosign.pub", "HostRelease public key"
        )
        software_key = _public_key(
            root / "software-release-cosign.pub", "SoftwareRelease public key"
        )
        if host_key == software_key:
            raise ValueError("HostRelease and SoftwareRelease trust roots must differ")
        if inventory.get("host_signing_key_sha256") != _sha256(
            host_key
        ) or inventory.get("software_signing_key_sha256") != _sha256(software_key):
            raise ValueError("release public-key inventory digest mismatch")
    else:
        if cosign is None:
            raise ValueError("cosign is required to verify a completed release handoff")
        with tempfile.TemporaryDirectory(
            prefix="drycontroller-handoff-verify-"
        ) as temporary:
            empty = Path(temporary)
            prepare_pair(
                host_release=root / "host-release.json",
                software_release=root / "software-release.json",
                host_public_key=root / "host-release-cosign.pub",
                software_public_key=root / "software-release-cosign.pub",
                cosign=cosign,
                output_dir=empty,
            )
            generated_inventory = json.loads(
                (empty / "inventory.json").read_text(encoding="utf-8")
            )
            if inventory != generated_inventory:
                raise ValueError("release handoff metadata is not canonical")
    _scan_public_package(root, kind)
    return kind


def trust_preflight(
    *,
    host_public_key: Path,
    software_public_key: Path,
    cloud_url: str,
    cloud_api_key: str,
) -> None:
    host_key = _public_key(host_public_key, "HostRelease public key")
    software_key = _public_key(software_public_key, "SoftwareRelease public key")
    if host_key == software_key:
        raise ValueError("HostRelease and SoftwareRelease trust roots must differ")
    if (
        not cloud_api_key
        or len(cloud_api_key) > 4096
        or any(character.isspace() for character in cloud_api_key)
    ):
        raise ValueError("Cloud Ops release API key is unavailable or invalid")
    parsed = urllib.parse.urlsplit(cloud_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Cloud Ops release URL must be one HTTPS origin")
    request = urllib.request.Request(
        cloud_url.rstrip("/") + "/api/v1/release-trust/attestation",
        headers={"X-API-Key": cloud_api_key},
        method="GET",
    )
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=15) as response:
            payload = json.loads(response.read(64 * 1024))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Cloud release-trust attestation failed") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "pair_id",
        "handoff_tag",
        "inventory_sha256",
        "source_commit",
        "host_sha256",
        "software_sha256",
    }:
        raise ValueError("Cloud release-trust attestation schema is invalid")
    pair_id = payload.get("pair_id")
    if (
        payload.get("schema") != "drycontroller.release-trust-attestation.v1"
        or not isinstance(pair_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", pair_id) is None
        or payload.get("inventory_sha256") != f"sha256:{pair_id}"
        or payload.get("handoff_tag")
        != f"drycontroller-release-public-keys-sha256-{pair_id}"
        or not isinstance(payload.get("source_commit"), str)
        or COMMIT_RE.fullmatch(payload["source_commit"]) is None
        or payload.get("host_sha256") != _sha256(host_key)
        or payload.get("software_sha256") != _sha256(software_key)
    ):
        raise ValueError(
            "Cloud release-trust attestation does not match CI signing keys"
        )


def _release_archive(root: Path, kind: str) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in _expected_files(kind):
            raw = _read_limited(root / name, MAX_RELEASE_BYTES, name)
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            member.mode = 0o644
            member.uid = 0
            member.gid = 0
            member.uname = ""
            member.gname = ""
            member.mtime = 0
            archive.addfile(member, io.BytesIO(raw))
    raw = stream.getvalue()
    if not raw or len(raw) > MAX_RELEASE_BYTES:
        raise ValueError("release handoff archive size is invalid")
    return raw


def _release_identity(root: Path, kind: str) -> tuple[str, str, dict[str, bytes]]:
    inventory_raw = _read_limited(
        root / "inventory.json", MAX_RELEASE_BYTES, "handoff inventory"
    )
    inventory = json.loads(inventory_raw)
    if not isinstance(inventory, dict) or inventory.get("kind") != kind:
        raise ValueError("release handoff inventory identity is invalid")
    source_commit = inventory.get("source_commit")
    if not isinstance(source_commit, str) or COMMIT_RE.fullmatch(source_commit) is None:
        raise ValueError("release handoff source commit is invalid")
    inventory_digest = hashlib.sha256(inventory_raw).hexdigest()
    tag = f"drycontroller-{kind}-sha256-{inventory_digest}"
    if RELEASE_TAG_RE.fullmatch(tag) is None:
        raise ValueError("release tag identity is invalid")
    archive = _release_archive(root, kind)
    checksum = f"{hashlib.sha256(archive).hexdigest()}  {RELEASE_ARCHIVE}\n".encode()
    return (
        tag,
        source_commit,
        {
            RELEASE_ARCHIVE: archive,
            RELEASE_CHECKSUM: checksum,
            "inventory.json": inventory_raw,
        },
    )


def _api_json(
    opener: urllib.request.OpenerDirector,
    *,
    url: str,
    token: str,
    method: str,
    body: dict[str, object] | None = None,
    expected: set[int],
) -> tuple[int, object | None]:
    data = None if body is None else _canonical_json(body)
    headers = {"Authorization": f"token {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=60) as response:
            status = response.status
            raw = response.read(MAX_RELEASE_BYTES)
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read(MAX_RELEASE_BYTES)
    except OSError as exc:
        raise ValueError("Gitea Release API request failed") from exc
    if status not in expected:
        raise ValueError(f"Gitea Release API returned unexpected HTTP {status}")
    if not raw:
        return status, None
    try:
        return status, json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Gitea Release API returned invalid JSON") from exc


def _validate_release(
    value: object, *, tag: str, source_commit: str
) -> tuple[int, list[object]]:
    if not isinstance(value, dict):
        raise ValueError("Gitea release response is invalid")
    release_id = value.get("id")
    assets = value.get("assets")
    if (
        not isinstance(release_id, int)
        or isinstance(release_id, bool)
        or release_id < 1
        or value.get("tag_name") != tag
        or value.get("target_commitish") != source_commit
        or value.get("draft") is not False
        or value.get("prerelease") is not False
        or not isinstance(assets, list)
    ):
        raise ValueError("Gitea release identity does not match the handoff")
    return release_id, assets


def _multipart(name: str, raw: bytes) -> tuple[bytes, str]:
    boundary = "drycontroller-" + os.urandom(16).hex()
    encoded_name = name.replace('"', "")
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="attachment"; filename="{encoded_name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + raw
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


def _same_asset(
    opener: urllib.request.OpenerDirector,
    *,
    asset: object,
    name: str,
    expected: bytes,
    token: str,
) -> bool:
    if not isinstance(asset, dict) or asset.get("name") != name:
        return False
    url = asset.get("browser_download_url")
    if not isinstance(url, str) or int(asset.get("size") or -1) != len(expected):
        raise ValueError(f"existing Gitea release asset differs: {name}")
    parsed = urllib.parse.urlsplit(url)
    approved = urllib.parse.urlsplit(APPROVED_GITEA_BASE_URL)
    if (
        parsed.scheme != approved.scheme
        or parsed.netloc != approved.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("Gitea release asset URL is outside the approved origin")
    request = urllib.request.Request(
        url, headers={"Authorization": f"token {token}"}, method="GET"
    )
    try:
        with opener.open(request, timeout=60) as response:
            observed = response.read(MAX_RELEASE_BYTES + 1)
    except (OSError, urllib.error.HTTPError) as exc:
        raise ValueError(f"cannot verify existing Gitea release asset: {name}") from exc
    if observed != expected:
        raise ValueError(f"existing Gitea release asset differs: {name}")
    return True


def publish(root: Path, *, token: str, cosign: Path | None) -> str:
    if (
        not token
        or len(token) > 4096
        or any(character.isspace() for character in token)
    ):
        raise ValueError("GITEA_TOKEN is unavailable or invalid")
    kind = verify_package(root, cosign=cosign)
    tag, source_commit, payloads = _release_identity(root, kind)
    opener = urllib.request.build_opener(_RejectRedirects())
    repository = (
        f"{APPROVED_GITEA_BASE_URL}/api/v1/repos/{GITEA_OWNER}/{GITEA_REPOSITORY}"
    )
    release_url = f"{repository}/releases/tags/{urllib.parse.quote(tag, safe='')}"
    status, release = _api_json(
        opener,
        url=release_url,
        token=token,
        method="GET",
        expected={200, 404},
    )
    if status == 404:
        _status, release = _api_json(
            opener,
            url=f"{repository}/releases",
            token=token,
            method="POST",
            body={
                "body": "DryController public release handoff; inventory digest is bound in the tag.",
                "draft": False,
                "name": tag,
                "prerelease": False,
                "tag_name": tag,
                "target_commitish": source_commit,
            },
            expected={201, 409},
        )
        if _status == 409:
            _status, release = _api_json(
                opener,
                url=release_url,
                token=token,
                method="GET",
                expected={200},
            )
    release_id, assets = _validate_release(
        release, tag=tag, source_commit=source_commit
    )
    by_name: dict[str, object] = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise ValueError("Gitea release asset inventory is invalid")
        name = asset["name"]
        if name in by_name:
            raise ValueError("Gitea release contains duplicate asset names")
        by_name[name] = asset
    unexpected = set(by_name) - set(RELEASE_ASSETS)
    if unexpected:
        raise ValueError("Gitea release contains non-handoff assets")
    if "inventory.json" in by_name and set(by_name) != set(RELEASE_ASSETS):
        raise ValueError("completed Gitea release is missing handoff assets")

    for name in RELEASE_ASSETS:
        raw = payloads[name]
        existing = by_name.get(name)
        if existing is not None:
            _same_asset(opener, asset=existing, name=name, expected=raw, token=token)
            continue
        body, content_type = _multipart(name, raw)
        request = urllib.request.Request(
            f"{repository}/releases/{release_id}/assets?name={urllib.parse.quote(name, safe='')}",
            data=body,
            headers={"Authorization": f"token {token}", "Content-Type": content_type},
            method="POST",
        )
        try:
            with opener.open(request, timeout=60) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            raise ValueError(
                f"Gitea rejected release asset {name} with HTTP {exc.code}"
            ) from exc
        except OSError as exc:
            raise ValueError(f"Gitea release asset upload failed: {name}") from exc
        if status != 201:
            raise ValueError(
                f"Gitea returned unexpected HTTP {status} for release asset {name}"
            )
    return tag


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    keys = subparsers.add_parser("keys")
    keys.add_argument("--host-public-key", required=True, type=Path)
    keys.add_argument("--software-public-key", required=True, type=Path)
    keys.add_argument("--source-commit", required=True)
    keys.add_argument("--output-dir", required=True, type=Path)

    pair = subparsers.add_parser("pair")
    pair.add_argument("--host-release", required=True, type=Path)
    pair.add_argument("--software-release", required=True, type=Path)
    pair.add_argument("--host-public-key", required=True, type=Path)
    pair.add_argument("--software-public-key", required=True, type=Path)
    pair.add_argument("--cosign", required=True, type=Path)
    pair.add_argument("--output-dir", required=True, type=Path)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--directory", required=True, type=Path)
    verify.add_argument("--cosign", type=Path)

    preflight = subparsers.add_parser("trust-preflight")
    preflight.add_argument("--host-public-key", required=True, type=Path)
    preflight.add_argument("--software-public-key", required=True, type=Path)
    preflight.add_argument("--cloud-url", required=True)
    preflight.add_argument("--cloud-api-key", required=True)

    upload = subparsers.add_parser("publish")
    upload.add_argument("--directory", required=True, type=Path)
    upload.add_argument("--cosign", type=Path)

    args = parser.parse_args()
    try:
        if args.command == "keys":
            prepare_keys(
                host_public_key=args.host_public_key,
                software_public_key=args.software_public_key,
                source_commit=args.source_commit,
                output_dir=args.output_dir,
            )
        elif args.command == "pair":
            prepare_pair(
                host_release=args.host_release,
                software_release=args.software_release,
                host_public_key=args.host_public_key,
                software_public_key=args.software_public_key,
                cosign=args.cosign,
                output_dir=args.output_dir,
            )
        elif args.command == "verify":
            verify_package(args.directory, cosign=args.cosign)
        elif args.command == "trust-preflight":
            trust_preflight(
                host_public_key=args.host_public_key,
                software_public_key=args.software_public_key,
                cloud_url=args.cloud_url,
                cloud_api_key=args.cloud_api_key,
            )
        else:
            publish(
                args.directory,
                token=os.environ.get("GITEA_TOKEN", ""),
                cosign=args.cosign,
            )
    except (OSError, ValueError) as exc:
        print(f"release handoff failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
