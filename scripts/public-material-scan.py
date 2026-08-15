#!/usr/bin/env python3
"""Fail-closed public-material scanner for OS Installer release artifacts."""

from __future__ import annotations

import argparse
import base64
import binascii
import bz2
import contextlib
import gzip
import hashlib
import json
import lzma
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
RUNNER_DIR = ROOT / "infra" / "host-release-runner"
BOOTSTRAP_DIR = ROOT / "infra" / "bootstrap"
if str(RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(RUNNER_DIR))
if str(BOOTSTRAP_DIR) not in sys.path:
    sys.path.insert(0, str(BOOTSTRAP_DIR))

from host_release_engine import (  # noqa: E402
    HostReleaseEngine,
    HostReleaseError,
    _docker_archive_digests,
    _local_image_reference,
    canonical_digest,
    canonical_json_bytes,
    validate_host_manifest,
    validate_software_manifest,
)
from installer_public_config import validate_public_config  # noqa: E402


SCHEMA = "drycontroller.public-material-scan.v1"
SCANNER_VERSION = "1.2.0"
SURFACES = ("source", "host", "software", "iso", "filesystem")
APPROVED_BASE_ISO_SHA256S = frozenset(
    {"e907d92eeec9df64163a7e454cbc8d7755e8ddc7ed42f99dbc80c40f1a138433"}
)
POLICY = {
    "schema": "drycontroller.public-material-policy.v1",
    "rules": [
        "CREDENTIAL-FILE",
        "RETIRED-FACTORY-MATERIAL",
        "PRIVATE-KEY",
        "TAILSCALE-AUTH-KEY",
        "CLOUD-ACCESS-KEY",
        "KNOWN-API-TOKEN",
        "REGISTRY-AUTH",
        "SECRET-ASSIGNMENT",
        "PASSWORD-HASH",
        "PUBLIC-MATERIAL-TYPE",
        "SCAN-UNSAFE-PATH",
        "SCAN-UNKNOWN-ARCHIVE",
        "SCAN-UNSUPPORTED-ENTRY",
        "SCAN-RESOURCE-LIMIT",
        "SCAN-TIMEOUT",
        "SCAN-VERIFICATION-FAILED",
        "SCAN-ERROR",
        "SCAN-ARTIFACT-CHANGED",
    ],
    "limits": {
        "maximum_archive_depth": 8,
        "maximum_entries": 2_000_000,
        "maximum_member_bytes": 16 * 1024 * 1024 * 1024,
        "maximum_total_bytes": 128 * 1024 * 1024 * 1024,
        "maximum_line_bytes": 64 * 1024,
    },
    "approved_base_iso_sha256s": sorted(APPROVED_BASE_ISO_SHA256S),
    "public_content_allowlist": {
        "host-release-cosign.pub": "cosign-public-key",
        "software-release-cosign.pub": "cosign-public-key",
        "device-identity-cloud.pub": "ed25519-public-key",
        "public-config.json": "installer-public-config",
        "identity-wheelhouse/SHA256SUMS": "sha256-digest-list",
        "host-manifest.sigstore.json": "sigstore-bundle",
        "software-manifest.sigstore.json": "sigstore-bundle",
    },
}
POLICY_SHA256 = hashlib.sha256(
    json.dumps(POLICY, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()

MAX_ARCHIVE_DEPTH = int(POLICY["limits"]["maximum_archive_depth"])
MAX_ENTRIES = int(POLICY["limits"]["maximum_entries"])
MAX_MEMBER_BYTES = int(POLICY["limits"]["maximum_member_bytes"])
MAX_TOTAL_BYTES = int(POLICY["limits"]["maximum_total_bytes"])
MAX_LINE_BYTES = int(POLICY["limits"]["maximum_line_bytes"])
CHUNK_BYTES = 1024 * 1024
OVERLAP_BYTES = MAX_LINE_BYTES

PRIVATE_KEY_RE = re.compile(
    br"-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED |ENCRYPTED COSIGN )?PRIVATE KEY-----"
)
TAILSCALE_KEY_RE = re.compile(br"\btskey-(?:auth|api)-[A-Za-z0-9_-]{16,}\b")
CLOUD_KEY_RES = (
    re.compile(br"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(br"\bAKID[A-Za-z0-9]{28,}\b"),
    re.compile(br"\bLTAI[A-Za-z0-9]{16,}\b"),
    re.compile(br"(?i)\bAccountKey=[A-Za-z0-9+/]{32,}={0,2}"),
)
KNOWN_TOKEN_RE = re.compile(
    br"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|"
    br"glpat-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|"
    br"sk_live_[A-Za-z0-9]{16,})\b"
)
BEARER_RE = re.compile(br"(?i)\bBearer[ \t]+[A-Za-z0-9._~+/-]{20,}={0,2}")
DOCKER_AUTH_RE = re.compile(br'(?i)["\']auth["\']\s*:\s*["\'][A-Za-z0-9+/]{16,}={0,2}["\']')
PASSWORD_HASH_RES = (
    re.compile(br"(?<![A-Za-z0-9])\$6\$[A-Za-z0-9./]{1,32}\$[A-Za-z0-9./]{32,}"),
    re.compile(br"(?<![A-Za-z0-9])\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}"),
    re.compile(br"(?<![A-Za-z0-9])\$y\$[A-Za-z0-9./$]{20,}"),
    re.compile(br"(?<![A-Za-z0-9])\$argon2(?:id|i|d)\$[^\s:]{20,}"),
)
SENSITIVE_KEY = br"[A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|credential)[A-Za-z0-9_.-]*"
LINE_ASSIGNMENT_RE = re.compile(
    br"(?im)^[ \t]*(?:export[ \t]+)?(?P<key>" + SENSITIVE_KEY + br")"
    br"[ \t]*(?P<separator>=|:)[ \t]*(?P<value>[^\r\n#]{0,2048})"
)
QUOTED_ASSIGNMENT_RE = re.compile(
    br"(?i)[\"'](?P<key>" + SENSITIVE_KEY + br")[\"']\s*:\s*"
    br"(?P<quote>[\"'])(?P<value>[^\"']{0,2048})(?P=quote)"
)
INLINE_ASSIGNMENT_RE = re.compile(
    br"(?i)(?P<key>" + SENSITIVE_KEY + br")\s*=\s*(?P<value>[^\s;&|]{1,2048})"
)

CREDENTIAL_BASENAMES = {
    ".env",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "application_default_credentials.json",
}
RETIRED_BASENAMES = {
    "activation-bundle.json",
    "factory-bundle.json",
    "factory-license.json",
    "password.env",
    "tailnet.env",
    "wifi.env",
}
PRIVATE_KEY_BASENAMES = {
    "cosign.key",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
}
TAR_SUFFIXES = (
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.xz",
    ".txz",
    ".tar.bz2",
    ".tbz",
    ".tbz2",
)
UNSUPPORTED_ARCHIVE_SUFFIXES = (
    ".7z",
    ".lz",
    ".lz4",
    ".lzma",
    ".lzo",
    ".rar",
    ".rpm",
    ".tar.lz4",
    ".tlz4",
    ".z",
)
CPIO_MAGICS = {b"070701", b"070702"}


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise TimeoutError


def sha256_file(path: Path, *, deadline: float | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            _check_deadline(deadline)
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactChangedError(RuntimeError):
    pass


class ArchiveInspectionError(RuntimeError):
    pass


@contextlib.contextmanager
def _artifact_snapshot(path: Path, *, deadline: float) -> Iterable[tuple[Path, str]]:
    """Copy, hash, and scan one stable descriptor; reject a changed live path."""
    with tempfile.TemporaryDirectory(prefix="drycontroller-public-scan-artifact-") as temporary:
        snapshot = Path(temporary) / path.name
        digest = hashlib.sha256()
        try:
            with path.open("rb") as source, snapshot.open("xb") as output:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ArtifactChangedError(path.name)
                for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
                    _check_deadline(deadline)
                    digest.update(chunk)
                    output.write(chunk)
                after = os.fstat(source.fileno())
        except TimeoutError:
            raise
        except OSError as exc:
            raise ArtifactChangedError(path.name) from exc
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if before_identity != after_identity:
            raise ArtifactChangedError(path.name)
        expected = digest.hexdigest()
        yield snapshot, expected
        try:
            current = sha256_file(path, deadline=deadline)
        except TimeoutError:
            raise
        except OSError as exc:
            raise ArtifactChangedError(path.name) from exc
        if current != expected:
            raise ArtifactChangedError(path.name)


@dataclass(frozen=True)
class Finding:
    rule_id: str
    archive: str
    path: str
    offset: int

    def as_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "archive": self.archive,
            "path": self.path,
            "offset": self.offset,
        }


class LimitExceeded(RuntimeError):
    def __init__(self, rule_id: str, archive: str, path: str) -> None:
        super().__init__(rule_id)
        self.rule_id = rule_id
        self.archive = archive
        self.path = path


class ScanState:
    def __init__(self, *, deadline: float) -> None:
        self.deadline = deadline
        self.entries = 0
        self.total_bytes = 0
        self.findings: list[Finding] = []
        self._seen: set[tuple[str, str, str, int]] = set()

    def add(self, rule_id: str, archive: str, path: str, offset: int = 0) -> None:
        key = (rule_id, archive, path, max(0, offset))
        if key not in self._seen:
            self._seen.add(key)
            self.findings.append(Finding(*key))

    def check(self, archive: str, path: str, *, size: int = 0, entry: bool = False) -> None:
        if time.monotonic() > self.deadline:
            raise LimitExceeded("SCAN-TIMEOUT", archive, path)
        if size < 0 or size > MAX_MEMBER_BYTES:
            raise LimitExceeded("SCAN-RESOURCE-LIMIT", archive, path)
        self.total_bytes += size
        if self.total_bytes > MAX_TOTAL_BYTES:
            raise LimitExceeded("SCAN-RESOURCE-LIMIT", archive, path)
        if entry:
            self.entries += 1
            if self.entries > MAX_ENTRIES:
                raise LimitExceeded("SCAN-RESOURCE-LIMIT", archive, path)


def _safe_member_name(name: str, state: ScanState, archive: str) -> str | None:
    clean = name.removeprefix("./").rstrip("/")
    path = PurePosixPath(clean)
    if (
        not clean
        or "\\" in clean
        or "\x00" in clean
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        state.add("SCAN-UNSAFE-PATH", archive, name)
        return None
    return path.as_posix()


def _scan_filename(path: str, state: ScanState, archive: str) -> None:
    lower = path.lower()
    basename = PurePosixPath(lower).name
    if (
        basename in CREDENTIAL_BASENAMES
        or lower == ".docker/config.json"
        or lower.endswith("/.docker/config.json")
        or lower == ".aws/credentials"
        or lower.endswith("/.aws/credentials")
        or lower == ".kube/config"
        or lower.endswith("/.kube/config")
    ):
        state.add("CREDENTIAL-FILE", archive, path)
    if basename in RETIRED_BASENAMES or "activation-bundle.v1" in lower:
        state.add("RETIRED-FACTORY-MATERIAL", archive, path)
    if (
        basename in PRIVATE_KEY_BASENAMES
        or basename.endswith(".pem.private")
        or basename.endswith(".private")
    ):
        state.add("PRIVATE-KEY", archive, path)


def _placeholder(raw: bytes) -> bool:
    value = raw.strip().rstrip(b",").strip()
    if len(value) >= 2 and value[:1] == value[-1:] and value[:1] in {b"'", b'"'}:
        value = value[1:-1].strip()
    lower = value.lower()
    if lower in {
        b"",
        b"!",
        b"*",
        b"x",
        b"none",
        b"null",
        b"false",
        b"disabled",
        b"example",
        b"placeholder",
        b"public",
        b"redacted",
    }:
        return True
    if (
        (value.startswith(b"${") and value.endswith(b"}"))
        or re.fullmatch(br"\$[A-Za-z_][A-Za-z0-9_]*", value)
        or (value.startswith(b"<") and value.endswith(b">"))
        or b"replace_with" in lower
        or b"change_me" in lower
        or lower.startswith((b"your_", b"your-"))
    ):
        return True
    return False


def _literal_assignment_value(raw: bytes) -> bytes | None:
    value = raw.strip().rstrip(b",").strip()
    if not value:
        return value
    if len(value) >= 2 and value[:1] == value[-1:] and value[:1] in {b"'", b'"'}:
        return value[1:-1]
    if any(character in value for character in (b"(", b")", b"[", b"]", b"{", b"}")):
        return None
    return value


def _scan_assignments(
    raw: bytes,
    *,
    state: ScanState,
    archive: str,
    path: str,
    base_offset: int,
    inline: bool = False,
) -> None:
    for expression in (LINE_ASSIGNMENT_RE, QUOTED_ASSIGNMENT_RE):
        for match in expression.finditer(raw):
            key = match.group("key")
            value = match.group("value")
            separator = match.groupdict().get("separator")
            if key.lower().endswith(b"_header") and re.fullmatch(
                br"[Xx]-[A-Za-z0-9-]+", value.strip().rstrip(b",")
            ):
                continue
            if path.lower().endswith(".py"):
                if separator == b":" and b"=" not in value:
                    continue
                if separator == b":" and b"=" in value:
                    value = value.split(b"=", 1)[1]
                candidate = value.strip().rstrip(b",").strip()
                if re.fullmatch(br"[A-Za-z_][A-Za-z0-9_.]*", candidate):
                    continue
            literal = _literal_assignment_value(value)
            if literal is not None and not _placeholder(literal):
                state.add("SECRET-ASSIGNMENT", archive, path, base_offset + match.start("value"))
    if inline:
        for match in INLINE_ASSIGNMENT_RE.finditer(raw):
            literal = _literal_assignment_value(match.group("value"))
            if literal is not None and not _placeholder(literal):
                state.add("SECRET-ASSIGNMENT", archive, path, base_offset + match.start("value"))


def _scan_chunk(raw: bytes, *, state: ScanState, archive: str, path: str, base_offset: int) -> None:
    for match in PRIVATE_KEY_RE.finditer(raw):
        state.add("PRIVATE-KEY", archive, path, base_offset + match.start())
    for match in TAILSCALE_KEY_RE.finditer(raw):
        state.add("TAILSCALE-AUTH-KEY", archive, path, base_offset + match.start())
    for expression in CLOUD_KEY_RES:
        for match in expression.finditer(raw):
            state.add("CLOUD-ACCESS-KEY", archive, path, base_offset + match.start())
    for expression, rule_id in (
        (KNOWN_TOKEN_RE, "KNOWN-API-TOKEN"),
        (BEARER_RE, "KNOWN-API-TOKEN"),
        (DOCKER_AUTH_RE, "REGISTRY-AUTH"),
    ):
        for match in expression.finditer(raw):
            state.add(rule_id, archive, path, base_offset + match.start())
    for expression in PASSWORD_HASH_RES:
        for match in expression.finditer(raw):
            state.add("PASSWORD-HASH", archive, path, base_offset + match.start())
    _scan_assignments(
        raw,
        state=state,
        archive=archive,
        path=path,
        base_offset=base_offset,
    )


def _scan_stream(
    stream: BinaryIO,
    *,
    state: ScanState,
    archive: str,
    path: str,
    initial: bytes = b"",
    count_bytes: bool = False,
) -> None:
    offset = 0
    overlap = b""
    pending = initial
    while True:
        state.check(archive, path)
        chunk = pending or stream.read(CHUNK_BYTES)
        pending = b""
        if not chunk:
            break
        if count_bytes:
            state.check(archive, path, size=len(chunk))
        combined = overlap + chunk
        base_offset = offset - len(overlap)
        _scan_chunk(combined, state=state, archive=archive, path=path, base_offset=base_offset)
        overlap = combined[-OVERLAP_BYTES:]
        offset += len(chunk)


def _archive_kind(path: str, head: bytes) -> str | None:
    lower = path.lower()
    if lower.endswith(TAR_SUFFIXES):
        return "tar"
    if lower.endswith((".tar.zst", ".tzst")):
        return "zstd"
    if lower.endswith((".whl", ".zip")):
        return "zip"
    if lower.endswith((".deb", ".ar")):
        return "ar"
    if lower.endswith((".cpio", ".newc")):
        return "cpio"
    if lower.endswith((".squashfs", ".sqfs")):
        return "squashfs"
    if lower.endswith(".gz"):
        return "gzip"
    if lower.endswith(".xz"):
        return "xz"
    if lower.endswith((".bz2", ".bzip2")):
        return "bzip2"
    if lower.endswith((".zst", ".zstd")):
        return "zstd"
    if lower.endswith(UNSUPPORTED_ARCHIVE_SUFFIXES):
        return "unsupported"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if head.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if head.startswith(b"BZh"):
        return "bzip2"
    if head.startswith(b"\x28\xb5\x2f\xfd"):
        return "zstd"
    if head.startswith(b"!<arch>\n"):
        return "ar"
    if head[:6] in CPIO_MAGICS:
        return "cpio"
    if head.startswith((b"070707", b"\xc7\x71", b"\x71\xc7")):
        return "unsupported"
    if head.startswith((b"hsqs", b"sqsh")):
        return "squashfs"
    if head.startswith((b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07")):
        return "unsupported"
    if head.startswith((b"\x04\x22\x4d\x18", b"\x02\x21\x4c\x18")) or (
        len(head) >= 4 and 0x50 <= head[0] <= 0x5F and head[1:4] == b"\x2a\x4d\x18"
    ):
        return "unsupported"
    if head.startswith((b"\x89LZO\x00\x0d\x0a\x1a\x0a", b"LZIP", b"\x1f\x9d")):
        return "unsupported"
    if len(head) > 262 and head[257:262] == b"ustar":
        return "tar"
    return None


def _validate_public_material(raw: bytes, *, state: ScanState, archive: str, path: str) -> None:
    kind = POLICY["public_content_allowlist"].get(path)
    if kind is None:
        return
    try:
        if len(raw) > 1024 * 1024:
            raise ValueError
        if kind == "cosign-public-key":
            if not (
                raw.startswith(b"-----BEGIN PUBLIC KEY-----\n")
                and raw.rstrip().endswith(b"-----END PUBLIC KEY-----")
            ):
                raise ValueError
        elif kind == "ed25519-public-key":
            if len(base64.b64decode(raw.decode("ascii").strip(), validate=True)) != 32:
                raise ValueError
        elif kind == "installer-public-config":
            validate_public_config(json.loads(raw))
        elif kind == "sha256-digest-list":
            lines = raw.decode("utf-8").splitlines()
            if not lines or any(re.fullmatch(r"[0-9a-f]{64}  [^\r\n]+", line) is None for line in lines):
                raise ValueError
        elif kind == "sigstore-bundle":
            if not isinstance(json.loads(raw), dict):
                raise ValueError
        else:
            raise ValueError
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        state.add("PUBLIC-MATERIAL-TYPE", archive, path)


def _decompressed_name(path: str, kind: str) -> str:
    lower = path.lower()
    suffixes = {
        "gzip": (".gz",),
        "xz": (".xz",),
        "bzip2": (".bz2", ".bzip2"),
        "zstd": (".zst", ".zstd"),
    }
    for suffix in suffixes[kind]:
        if lower.endswith(suffix):
            return path[: -len(suffix)]
    return f"{path}@decompressed"


def _copy_decompressed(
    source: BinaryIO,
    output: BinaryIO,
    *,
    state: ScanState,
    archive: str,
    path: str,
) -> int:
    size = 0
    while True:
        state.check(archive, path)
        chunk = source.read(CHUNK_BYTES)
        if not chunk:
            return size
        size += len(chunk)
        if size > MAX_MEMBER_BYTES:
            raise LimitExceeded("SCAN-RESOURCE-LIMIT", archive, path)
        state.check(archive, path, size=len(chunk))
        output.write(chunk)


def _decompress_to_spool(
    compressed: BinaryIO,
    output: BinaryIO,
    *,
    kind: str,
    state: ScanState,
    archive: str,
    path: str,
) -> int:
    compressed.seek(0)
    if kind in {"gzip", "xz", "bzip2"}:
        if kind == "gzip":
            source_context = gzip.GzipFile(fileobj=compressed, mode="rb")
        elif kind == "xz":
            source_context = lzma.LZMAFile(compressed, mode="rb")
        else:
            source_context = bz2.BZ2File(compressed, mode="rb")
        with source_context as source:
            return _copy_decompressed(
                source,
                output,
                state=state,
                archive=archive,
                path=path,
            )

    zstd = shutil.which("zstd")
    if zstd is None:
        raise ArchiveInspectionError("zstd unavailable")
    process = subprocess.Popen(
        [zstd, "-q", "-d", "-c"],
        stdin=compressed,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    try:
        size = _copy_decompressed(
            process.stdout,
            output,
            state=state,
            archive=archive,
            path=path,
        )
        available = state.deadline - time.monotonic()
        if available <= 0:
            raise LimitExceeded("SCAN-TIMEOUT", archive, path)
        if process.wait(timeout=available) != 0:
            raise ArchiveInspectionError("zstd decompression failed")
        return size
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()


def _scan_ar(
    archive_file: BinaryIO,
    *,
    state: ScanState,
    archive: str,
    depth: int,
) -> None:
    archive_file.seek(0)
    if archive_file.read(8) != b"!<arch>\n":
        raise ArchiveInspectionError("ar header invalid")
    index = 0
    while True:
        header = archive_file.read(60)
        if not header:
            return
        if len(header) != 60 or header[58:60] != b"`\n":
            raise ArchiveInspectionError("ar member header invalid")
        try:
            stored_size = int(header[48:58].decode("ascii").strip())
            raw_name = header[:16].decode("utf-8").strip()
        except (UnicodeDecodeError, ValueError) as exc:
            raise ArchiveInspectionError("ar member metadata invalid") from exc
        if stored_size < 0 or stored_size > MAX_MEMBER_BYTES:
            raise LimitExceeded("SCAN-RESOURCE-LIMIT", archive, f"ar-member-{index}")
        name_prefix = b""
        if raw_name.startswith("#1/"):
            try:
                name_size = int(raw_name[3:])
            except ValueError as exc:
                raise ArchiveInspectionError("ar member name invalid") from exc
            if name_size < 1 or name_size > stored_size:
                raise ArchiveInspectionError("ar member name invalid")
            name_prefix = archive_file.read(name_size)
            if len(name_prefix) != name_size:
                raise ArchiveInspectionError("ar member truncated")
            try:
                member_name = name_prefix.rstrip(b"\x00").decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ArchiveInspectionError("ar member name invalid") from exc
        else:
            member_name = raw_name.rstrip("/") or f"ar-member-{index}"
        data_size = stored_size - len(name_prefix)
        safe_name = _safe_member_name(member_name, state, archive)
        with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as member_file:
            remaining = data_size
            while remaining:
                chunk = archive_file.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise ArchiveInspectionError("ar member truncated")
                member_file.write(chunk)
                remaining -= len(chunk)
            if safe_name is not None:
                member_file.seek(0)
                _scan_regular_stream(
                    member_file,
                    size=data_size,
                    state=state,
                    archive=archive,
                    path=safe_name,
                    depth=depth,
                )
        if stored_size % 2 and len(archive_file.read(1)) != 1:
            raise ArchiveInspectionError("ar member padding missing")
        index += 1


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    raw = stream.read(size)
    if len(raw) != size:
        raise ArchiveInspectionError("archive member truncated")
    return raw


def _copy_exact_to_spool(
    source: BinaryIO,
    output: BinaryIO,
    *,
    size: int,
    state: ScanState,
    archive: str,
    path: str,
) -> int:
    checksum = 0
    remaining = size
    while remaining:
        state.check(archive, path)
        chunk = source.read(min(CHUNK_BYTES, remaining))
        if not chunk:
            raise ArchiveInspectionError("archive member truncated")
        output.write(chunk)
        checksum = (checksum + sum(chunk)) & 0xFFFFFFFF
        remaining -= len(chunk)
    return checksum


def _scan_cpio_trailing(
    source: BinaryIO,
    *,
    state: ScanState,
    archive: str,
    path: str,
    depth: int,
) -> None:
    with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as trailing:
        size = 0
        found = False
        while True:
            state.check(archive, path)
            chunk = source.read(CHUNK_BYTES)
            if not chunk:
                break
            if not found:
                stripped = chunk.lstrip(b"\0")
                if not stripped:
                    continue
                chunk = stripped
                found = True
            size += len(chunk)
            if size > MAX_MEMBER_BYTES:
                raise LimitExceeded("SCAN-RESOURCE-LIMIT", archive, path)
            trailing.write(chunk)
        if not found:
            return
        trailing.seek(0)
        _scan_regular_stream(
            trailing,
            size=size,
            state=state,
            archive=archive,
            path=f"{path}@trailing",
            depth=depth + 1,
        )


def _scan_cpio(
    archive_file: BinaryIO,
    *,
    state: ScanState,
    archive: str,
    path: str,
    depth: int,
) -> None:
    archive_file.seek(0)
    while True:
        state.check(archive, path)
        magic = archive_file.read(6)
        if not magic:
            raise ArchiveInspectionError("newc trailer missing")
        if magic not in CPIO_MAGICS:
            raise ArchiveInspectionError("newc header invalid")
        header = _read_exact(archive_file, 104)
        try:
            fields = [int(header[index : index + 8], 16) for index in range(0, 104, 8)]
        except ValueError as exc:
            raise ArchiveInspectionError("newc metadata invalid") from exc
        mode = fields[1]
        size = fields[6]
        name_size = fields[11]
        expected_checksum = fields[12]
        if name_size < 1 or name_size > MAX_LINE_BYTES or size > MAX_MEMBER_BYTES:
            raise LimitExceeded("SCAN-RESOURCE-LIMIT", archive, path)
        raw_name = _read_exact(archive_file, name_size)
        if raw_name[-1:] != b"\0" or b"\0" in raw_name[:-1]:
            raise ArchiveInspectionError("newc member name invalid")
        try:
            name = raw_name[:-1].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArchiveInspectionError("newc member name invalid") from exc
        header_padding = (-(110 + name_size)) % 4
        if header_padding and any(_read_exact(archive_file, header_padding)):
            raise ArchiveInspectionError("newc header padding invalid")
        safe_name = _safe_member_name(name, state, archive)
        with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as member_file:
            checksum = _copy_exact_to_spool(
                archive_file,
                member_file,
                size=size,
                state=state,
                archive=archive,
                path=name,
            )
            data_padding = (-size) % 4
            if data_padding and any(_read_exact(archive_file, data_padding)):
                raise ArchiveInspectionError("newc data padding invalid")
            if magic == b"070702" and checksum != expected_checksum:
                raise ArchiveInspectionError("newc checksum invalid")
            if magic == b"070701" and expected_checksum != 0:
                raise ArchiveInspectionError("newc checksum field invalid")
            if name == "TRAILER!!!":
                if size != 0:
                    raise ArchiveInspectionError("newc trailer invalid")
                _scan_cpio_trailing(
                    archive_file,
                    state=state,
                    archive=archive,
                    path=path,
                    depth=depth,
                )
                return
            if safe_name is None:
                continue
            file_type = stat.S_IFMT(mode)
            if file_type == stat.S_IFREG:
                member_file.seek(0)
                _scan_regular_stream(
                    member_file,
                    size=size,
                    state=state,
                    archive=archive,
                    path=safe_name,
                    depth=depth,
                )
            elif file_type == stat.S_IFDIR:
                state.check(archive, safe_name, entry=True)
                if size != 0:
                    state.add("SCAN-UNSUPPORTED-ENTRY", archive, safe_name)
            elif file_type == stat.S_IFLNK:
                state.check(archive, safe_name, size=size, entry=True)
                member_file.seek(0)
                _scan_stream(
                    member_file,
                    state=state,
                    archive=archive,
                    path=f"{safe_name}@link-target",
                )
            else:
                state.check(archive, safe_name, size=size, entry=True)
                state.add("SCAN-UNSUPPORTED-ENTRY", archive, safe_name)


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except PermissionError:
            process.kill()
        except ProcessLookupError:
            return
    process.wait()


def _run_command_lines(
    command: list[str],
    *,
    deadline: float,
    consume: Callable[[bytes], None],
) -> None:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert process.stdout is not None
    buffer = b""
    try:
        while True:
            available = deadline - time.monotonic()
            if available <= 0:
                raise LimitExceeded("SCAN-TIMEOUT", command[0], command[-1])
            readable, _, _ = select.select([process.stdout], [], [], available)
            if not readable:
                raise LimitExceeded("SCAN-TIMEOUT", command[0], command[-1])
            chunk = os.read(process.stdout.fileno(), CHUNK_BYTES)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if len(line) > MAX_LINE_BYTES:
                    raise ArchiveInspectionError("archive listing line too long")
                consume(line)
            if len(buffer) > MAX_LINE_BYTES:
                raise ArchiveInspectionError("archive listing line too long")
        if buffer:
            consume(buffer)
        available = deadline - time.monotonic()
        if available <= 0:
            raise LimitExceeded("SCAN-TIMEOUT", command[0], command[-1])
        try:
            returncode = process.wait(timeout=available)
        except subprocess.TimeoutExpired as exc:
            raise LimitExceeded("SCAN-TIMEOUT", command[0], command[-1]) from exc
        if returncode != 0:
            raise ArchiveInspectionError("archive listing failed")
    finally:
        process.stdout.close()
        _terminate_process(process)


def _preflight_squashfs(
    image: Path,
    *,
    state: ScanState,
    archive: str,
    path: str,
) -> None:
    entries = 0
    total = 0

    def consume(raw: bytes) -> None:
        nonlocal entries, total
        state.check(archive, path)
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArchiveInspectionError("SquashFS listing is not UTF-8") from exc
        if len(line) < 11 or line[0] not in "-dlcbps" or line[10] != " ":
            return
        fields = line.split(maxsplit=5)
        if len(fields) != 6:
            raise ArchiveInspectionError("SquashFS listing metadata invalid")
        entries += 1
        if entries > MAX_ENTRIES:
            raise LimitExceeded("SCAN-RESOURCE-LIMIT", archive, path)
        if line[0] in "-l":
            try:
                size = int(fields[2])
            except ValueError as exc:
                raise ArchiveInspectionError("SquashFS member size invalid") from exc
            if size < 0 or size > MAX_MEMBER_BYTES:
                raise LimitExceeded("SCAN-RESOURCE-LIMIT", archive, path)
            total += size
            if total > MAX_TOTAL_BYTES:
                raise LimitExceeded("SCAN-RESOURCE-LIMIT", archive, path)
        elif line[0] not in "d":
            raise ArchiveInspectionError("SquashFS contains unsupported entry")

    _run_command_lines(
        ["unsquashfs", "-lln", "-no-progress", str(image)],
        deadline=state.deadline,
        consume=consume,
    )
    if entries == 0:
        raise ArchiveInspectionError("SquashFS listing is empty")


def _preflight_tree(root: Path, *, state: ScanState, archive: str) -> None:
    entries = 0
    total = 0
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        state.check(archive, str(directory))
        current = Path(directory)
        for name in [*names, *files]:
            candidate = current / name
            metadata = candidate.lstat()
            entries += 1
            if entries > MAX_ENTRIES:
                raise LimitExceeded("SCAN-RESOURCE-LIMIT", archive, name)
            if stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                if metadata.st_size > MAX_MEMBER_BYTES:
                    raise LimitExceeded("SCAN-RESOURCE-LIMIT", archive, name)
                total += metadata.st_size
                if total > MAX_TOTAL_BYTES:
                    raise LimitExceeded("SCAN-RESOURCE-LIMIT", archive, name)
            elif not stat.S_ISDIR(metadata.st_mode):
                raise ArchiveInspectionError("extracted tree contains unsupported entry")


def _scan_squashfs(
    archive_file: BinaryIO,
    *,
    state: ScanState,
    archive: str,
    path: str,
    depth: int,
) -> None:
    unsquashfs = shutil.which("unsquashfs")
    if unsquashfs is None:
        raise ArchiveInspectionError("unsquashfs unavailable")
    with tempfile.TemporaryDirectory(prefix="drycontroller-public-scan-squashfs-") as temporary:
        directory = Path(temporary)
        image = directory / "filesystem.squashfs"
        rootfs = directory / "rootfs"
        archive_file.seek(0)
        with image.open("wb") as output:
            copied = 0
            while True:
                state.check(archive, path)
                chunk = archive_file.read(CHUNK_BYTES)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > MAX_MEMBER_BYTES:
                    raise LimitExceeded("SCAN-RESOURCE-LIMIT", archive, path)
                output.write(chunk)
        _preflight_squashfs(image, state=state, archive=archive, path=path)
        available = state.deadline - time.monotonic()
        if available <= 0:
            raise LimitExceeded("SCAN-TIMEOUT", archive, path)
        completed = subprocess.run(
            [unsquashfs, "-no-progress", "-no-xattrs", "-d", str(rootfs), str(image)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=available,
            check=False,
        )
        if completed.returncode != 0 or not rootfs.is_dir():
            raise ArchiveInspectionError("SquashFS extraction failed")
        _preflight_tree(rootfs, state=state, archive=f"{archive}!{path}")
        _scan_tree(rootfs, state=state, archive=f"{archive}!{path}", depth=depth + 1)


def _scan_regular_stream(
    stream: BinaryIO,
    *,
    size: int,
    state: ScanState,
    archive: str,
    path: str,
    depth: int,
) -> None:
    _scan_filename(path, state, archive)
    state.check(archive, path, size=size, entry=True)
    if depth > MAX_ARCHIVE_DEPTH:
        state.add("SCAN-RESOURCE-LIMIT", archive, path)
        return
    public_kind = POLICY["public_content_allowlist"].get(path)
    if public_kind is not None:
        if size > 1024 * 1024:
            state.add("PUBLIC-MATERIAL-TYPE", archive, path)
            return
        raw = stream.read(size + 1)
        if len(raw) != size:
            state.add("SCAN-ERROR", archive, path)
            return
        _validate_public_material(raw, state=state, archive=archive, path=path)
        _scan_chunk(raw, state=state, archive=archive, path=path, base_offset=0)
        return
    head = stream.read(min(512, size))
    kind = _archive_kind(path, head)
    if kind is None:
        _scan_stream(
            stream,
            state=state,
            archive=archive,
            path=path,
            initial=head,
        )
        return
    with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as spool:
        spool.write(head)
        remaining = size - len(head)
        while remaining:
            state.check(archive, path)
            chunk = stream.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                state.add("SCAN-ERROR", archive, path)
                return
            spool.write(chunk)
            remaining -= len(chunk)
        if stream.read(1):
            state.add("SCAN-RESOURCE-LIMIT", archive, path)
            return
        spool.seek(0)
        nested_archive = f"{archive}!{path}"
        try:
            _scan_stream(
                spool,
                state=state,
                archive=nested_archive,
                path=f"{path}@raw",
            )
            if kind == "unsupported":
                state.add("SCAN-UNKNOWN-ARCHIVE", archive, path)
                return
            spool.seek(0)
            if kind == "tar":
                with tarfile.open(fileobj=spool, mode="r:*") as nested:
                    _scan_tar(nested, state=state, archive=nested_archive, depth=depth + 1)
            elif kind == "zip":
                with zipfile.ZipFile(spool) as nested:
                    _scan_zip(nested, state=state, archive=nested_archive, depth=depth + 1)
            elif kind == "ar":
                _scan_ar(spool, state=state, archive=nested_archive, depth=depth + 1)
            elif kind == "cpio":
                _scan_cpio(
                    spool,
                    state=state,
                    archive=nested_archive,
                    path=path,
                    depth=depth + 1,
                )
            elif kind == "squashfs":
                _scan_squashfs(
                    spool,
                    state=state,
                    archive=nested_archive,
                    path=path,
                    depth=depth,
                )
            else:
                with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as decompressed:
                    decompressed_size = _decompress_to_spool(
                        spool,
                        decompressed,
                        kind=kind,
                        state=state,
                        archive=nested_archive,
                        path=path,
                    )
                    decompressed.seek(0)
                    _scan_regular_stream(
                        decompressed,
                        size=decompressed_size,
                        state=state,
                        archive=nested_archive,
                        path=_decompressed_name(path, kind),
                        depth=depth + 1,
                    )
        except subprocess.TimeoutExpired:
            state.add("SCAN-TIMEOUT", archive, path)
        except (
            ArchiveInspectionError,
            EOFError,
            gzip.BadGzipFile,
            lzma.LZMAError,
            OSError,
            tarfile.TarError,
            zipfile.BadZipFile,
        ):
            state.add("SCAN-UNKNOWN-ARCHIVE", archive, path)


def _scan_tar(archive_file: tarfile.TarFile, *, state: ScanState, archive: str, depth: int) -> None:
    for member in archive_file:
        state.check(archive, member.name, entry=True)
        path = _safe_member_name(member.name, state, archive)
        if path is None:
            continue
        _scan_filename(path, state, archive)
        if member.issym() or member.islnk():
            _scan_chunk(
                os.fsencode(member.linkname),
                state=state,
                archive=archive,
                path=f"{path}@link-target",
                base_offset=0,
            )
            continue
        if member.isdir():
            continue
        if not member.isfile():
            state.add("SCAN-UNSUPPORTED-ENTRY", archive, path)
            continue
        source = archive_file.extractfile(member)
        if source is None:
            state.add("SCAN-ERROR", archive, path)
            continue
        _scan_regular_stream(
            source,
            size=member.size,
            state=state,
            archive=archive,
            path=path,
            depth=depth,
        )


def _scan_zip(archive_file: zipfile.ZipFile, *, state: ScanState, archive: str, depth: int) -> None:
    for member in archive_file.infolist():
        state.check(archive, member.filename, entry=True)
        path = _safe_member_name(member.filename, state, archive)
        if path is None or member.is_dir():
            continue
        _scan_filename(path, state, archive)
        if member.file_size > MAX_MEMBER_BYTES:
            state.add("SCAN-RESOURCE-LIMIT", archive, path)
            continue
        with archive_file.open(member) as source:
            _scan_regular_stream(
                source,
                size=member.file_size,
                state=state,
                archive=archive,
                path=path,
                depth=depth,
            )


def _scan_tar_path(path: Path, *, state: ScanState, archive: str) -> None:
    try:
        with tarfile.open(path, mode="r:*") as source:
            _scan_tar(source, state=state, archive=archive, depth=1)
    except (EOFError, OSError, tarfile.TarError):
        state.add("SCAN-UNKNOWN-ARCHIVE", archive, path.name)


def _scan_raw_path(path: Path, *, state: ScanState, archive: str) -> None:
    try:
        state.check(archive, path.name, entry=True)
        with path.open("rb") as source:
            _scan_stream(
                source,
                state=state,
                archive=archive,
                path=f"{path.name}@raw",
                count_bytes=True,
            )
    except OSError:
        state.add("SCAN-ERROR", archive, path.name)


def _scan_file_path(
    path: Path,
    *,
    state: ScanState,
    archive: str,
    virtual_path: str,
    depth: int = 1,
) -> None:
    try:
        size = path.stat().st_size
        with path.open("rb") as source:
            _scan_regular_stream(
                source,
                size=size,
                state=state,
                archive=archive,
                path=virtual_path,
                depth=depth,
            )
    except OSError:
        state.add("SCAN-ERROR", archive, virtual_path)


def _tree_files(
    root: Path,
    state: ScanState,
    archive: str,
    *,
    inspect: bool = True,
) -> Iterable[tuple[str, Path]]:
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        state.check(archive, str(directory))
        current = Path(directory)
        safe_names: list[str] = []
        for name in sorted(names):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if inspect:
                _scan_filename(relative, state, archive)
            if path.is_symlink():
                if inspect:
                    try:
                        target = os.readlink(path)
                        _scan_chunk(
                            os.fsencode(target),
                            state=state,
                            archive=archive,
                            path=f"{relative}@link-target",
                            base_offset=0,
                        )
                        resolved = (path.parent / target).resolve(strict=False) if not os.path.isabs(target) else (root / target.lstrip("/")).resolve(strict=False)
                        resolved.relative_to(root.resolve())
                    except (OSError, ValueError):
                        state.add("SCAN-UNSAFE-PATH", archive, relative)
                continue
            safe_names.append(name)
        names[:] = safe_names
        for name in sorted(files):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if inspect:
                _scan_filename(relative, state, archive)
            if path.is_symlink():
                if inspect:
                    try:
                        target = os.readlink(path)
                        _scan_chunk(
                            os.fsencode(target),
                            state=state,
                            archive=archive,
                            path=f"{relative}@link-target",
                            base_offset=0,
                        )
                        resolved = (path.parent / target).resolve(strict=False) if not os.path.isabs(target) else (root / target.lstrip("/")).resolve(strict=False)
                        resolved.relative_to(root.resolve())
                    except (OSError, ValueError):
                        state.add("SCAN-UNSAFE-PATH", archive, relative)
                continue
            if path.is_file():
                yield relative, path
            else:
                if inspect:
                    state.add("SCAN-UNSUPPORTED-ENTRY", archive, relative)


def _scan_tree(
    root: Path,
    *,
    state: ScanState,
    archive: str,
    only: set[str] | None = None,
    depth: int = 1,
) -> None:
    for relative, path in _tree_files(root, state, archive):
        if only is None or relative in only:
            _scan_file_path(
                path,
                state=state,
                archive=archive,
                virtual_path=relative,
                depth=depth,
            )


def _artifact_result(digest: str, state: ScanState, **extra: object) -> dict[str, object]:
    findings = sorted(
        (finding.as_dict() for finding in state.findings),
        key=lambda item: (str(item["rule_id"]), str(item["archive"]), str(item["path"]), int(item["offset"])),
    )
    return {"sha256": digest, "passed": not findings, "findings": findings, **extra}


def scan_source(path: Path, *, deadline: float) -> dict[str, object]:
    state = ScanState(deadline=deadline)
    digest = ""
    try:
        with _artifact_snapshot(path, deadline=deadline) as (snapshot, digest):
            _scan_raw_path(snapshot, state=state, archive="source")
            _scan_tar_path(snapshot, state=state, archive="source")
    except ArtifactChangedError:
        state.add("SCAN-ARTIFACT-CHANGED", "source", path.name)
    except LimitExceeded as exc:
        state.add(exc.rule_id, exc.archive, exc.path)
    except TimeoutError:
        state.add("SCAN-TIMEOUT", "source", path.name)
    return _artifact_result(digest, state)


def _load_seed(path: Path, *, host: bool) -> tuple[dict[str, object], str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {
        "manifest",
        "manifest_digest",
        "manifest_signature",
    }:
        raise ValueError("seed fields invalid")
    manifest = validate_host_manifest(payload["manifest"]) if host else validate_software_manifest(payload["manifest"])
    if payload["manifest_digest"] != canonical_digest(manifest):
        raise ValueError("seed digest mismatch")
    signature = payload["manifest_signature"]
    try:
        decoded = base64.b64decode(signature, validate=True)
    except (TypeError, binascii.Error, ValueError) as exc:
        raise ValueError("seed signature invalid") from exc
    if not decoded or len(decoded) > 16 * 1024:
        raise ValueError("seed signature invalid")
    return manifest, signature


def _verify_seed(
    *,
    seed: Path,
    public_key: Path,
    cosign: Path,
    host: bool,
    deadline: float,
) -> dict[str, object]:
    manifest, signature = _load_seed(seed, host=host)
    key = public_key.read_bytes()
    if not key or len(key) > 64 * 1024:
        raise ValueError("release public key invalid")
    if manifest["signing_key_sha256"] != "sha256:" + hashlib.sha256(key).hexdigest():
        raise ValueError("release public key mismatch")
    available = deadline - time.monotonic()
    if available <= 0:
        raise TimeoutError
    remaining = max(1, min(120, int(available)))
    with tempfile.TemporaryDirectory(prefix="drycontroller-public-scan-signature-") as temporary:
        directory = Path(temporary)
        manifest_path = directory / "manifest.json"
        signature_path = directory / "manifest.sigstore.json"
        key_path = directory / "cosign.pub"
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        signature_path.write_bytes(base64.b64decode(signature, validate=True))
        key_path.write_bytes(key)
        completed = subprocess.run(
            [
                str(cosign),
                "verify-blob",
                "--key",
                str(key_path),
                "--insecure-ignore-tlog=true",
                "--bundle",
                str(signature_path),
                str(manifest_path),
            ],
            cwd=directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=remaining,
            check=False,
        )
    if completed.returncode != 0:
        raise ValueError("release signature invalid")
    return manifest


def scan_host(
    path: Path,
    *,
    seed: Path,
    public_key: Path,
    cosign: Path,
    deadline: float,
) -> dict[str, object]:
    state = ScanState(deadline=deadline)
    digest = ""
    seed_digest = ""
    public_key_digest = ""
    try:
        with (
            _artifact_snapshot(path, deadline=deadline) as (snapshot, digest),
            _artifact_snapshot(seed, deadline=deadline) as (seed_snapshot, seed_digest),
            _artifact_snapshot(public_key, deadline=deadline) as (public_key_snapshot, public_key_digest),
        ):
            manifest = _verify_seed(
                seed=seed_snapshot,
                public_key=public_key_snapshot,
                cosign=cosign,
                host=True,
                deadline=deadline,
            )
            with tempfile.TemporaryDirectory(prefix="drycontroller-public-scan-host-") as temporary:
                staging = Path(temporary)
                HostReleaseEngine()._extract_bundle(snapshot, manifest, staging)
                _scan_raw_path(snapshot, state=state, archive="host")
                _scan_tar_path(snapshot, state=state, archive="host")
                _scan_tree(staging / "rootfs", state=state, archive="host:merged-rootfs")
    except ArtifactChangedError:
        state.add("SCAN-ARTIFACT-CHANGED", "host", path.name)
    except LimitExceeded as exc:
        state.add(exc.rule_id, exc.archive, exc.path)
    except (TimeoutError, subprocess.TimeoutExpired):
        state.add("SCAN-TIMEOUT", "host", path.name)
    except (HostReleaseError, OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        state.add("SCAN-VERIFICATION-FAILED", "host", path.name)
    return _artifact_result(
        digest,
        state,
        seed_sha256=seed_digest,
        public_key_sha256=public_key_digest,
    )


def _remove_overlay_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _overlay_path_in_root(path: Path, rootfs: Path) -> Path:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(rootfs.resolve())
    except ValueError as exc:
        raise ValueError("Docker layer path escapes rootfs") from exc
    return resolved


def _prepare_overlay_parent(destination: Path, rootfs: Path) -> Path:
    parent = _overlay_path_in_root(destination.parent, rootfs)
    parent.mkdir(parents=True, exist_ok=True)
    return parent


def _apply_and_scan_layer(
    outer: tarfile.TarFile,
    layer_name: str,
    *,
    rootfs: Path,
    state: ScanState,
    archive: str,
) -> None:
    member = outer.getmember(layer_name)
    if not member.isfile() or member.size > MAX_MEMBER_BYTES:
        raise ValueError("Docker layer invalid")
    source = outer.extractfile(member)
    if source is None:
        raise ValueError("Docker layer missing")
    with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as spool:
        remaining = member.size
        while remaining:
            state.check(archive, layer_name)
            chunk = source.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                raise ValueError("Docker layer truncated")
            spool.write(chunk)
            remaining -= len(chunk)
        if source.read(1):
            raise ValueError("Docker layer size invalid")
        spool.seek(0)
        _scan_stream(spool, state=state, archive=archive, path=f"{layer_name}@raw")
        spool.seek(0)
        with tarfile.open(fileobj=spool, mode="r:*") as layer:
            for item in layer:
                state.check(archive, item.name, size=item.size if item.isfile() else 0, entry=True)
                relative = _safe_member_name(item.name, state, archive)
                if relative is None:
                    continue
                _scan_filename(relative, state, archive)
                pure = PurePosixPath(relative)
                basename = pure.name
                parent = rootfs.joinpath(*pure.parent.parts)
                if basename == ".wh..wh..opq":
                    resolved_parent = _overlay_path_in_root(parent, rootfs)
                    if resolved_parent.is_dir():
                        for child in resolved_parent.iterdir():
                            _remove_overlay_path(child)
                    continue
                if basename.startswith(".wh."):
                    resolved_parent = _overlay_path_in_root(parent, rootfs)
                    _remove_overlay_path(resolved_parent / basename.removeprefix(".wh."))
                    continue
                destination = rootfs.joinpath(*pure.parts)
                if item.isdir():
                    if destination.exists() and not destination.is_dir():
                        _remove_overlay_path(destination)
                    _prepare_overlay_parent(destination, rootfs)
                    _overlay_path_in_root(destination, rootfs).mkdir(parents=True, exist_ok=True)
                    continue
                if item.issym():
                    target = item.linkname
                    _scan_chunk(
                        os.fsencode(target),
                        state=state,
                        archive=archive,
                        path=f"{relative}@link-target",
                        base_offset=0,
                    )
                    if "\\" in target or "\x00" in target:
                        state.add("SCAN-UNSAFE-PATH", archive, relative)
                        continue
                    actual_parent = _prepare_overlay_parent(destination, rootfs)
                    if PurePosixPath(target).is_absolute():
                        target_path = rootfs.joinpath(*PurePosixPath(target).parts[1:])
                        link_target = os.path.relpath(target_path, actual_parent)
                    else:
                        target_path = actual_parent / target
                        link_target = target
                    try:
                        _overlay_path_in_root(target_path, rootfs)
                    except ValueError:
                        state.add("SCAN-UNSAFE-PATH", archive, relative)
                        continue
                    _remove_overlay_path(destination)
                    destination.symlink_to(link_target)
                    continue
                if item.islnk():
                    linked = _safe_member_name(item.linkname, state, archive)
                    if linked is None:
                        continue
                    source_path = _overlay_path_in_root(
                        rootfs.joinpath(*PurePosixPath(linked).parts),
                        rootfs,
                    )
                    if source_path.is_file():
                        _prepare_overlay_parent(destination, rootfs)
                        _remove_overlay_path(destination)
                        shutil.copyfile(source_path, destination)
                    else:
                        state.add("SCAN-UNSUPPORTED-ENTRY", archive, relative)
                    continue
                if not item.isfile():
                    state.add("SCAN-UNSUPPORTED-ENTRY", archive, relative)
                    continue
                file_source = layer.extractfile(item)
                if file_source is None:
                    state.add("SCAN-ERROR", archive, relative)
                    continue
                _prepare_overlay_parent(destination, rootfs)
                _remove_overlay_path(destination)
                with destination.open("wb") as output:
                    remaining = item.size
                    while remaining:
                        state.check(archive, relative)
                        chunk = file_source.read(min(CHUNK_BYTES, remaining))
                        if not chunk:
                            state.add("SCAN-ERROR", archive, relative)
                            break
                        output.write(chunk)
                        remaining -= len(chunk)
                if remaining == 0:
                    _scan_file_path(destination, state=state, archive=archive, virtual_path=relative)


def _scan_docker_config(
    raw: bytes,
    config: dict[str, object],
    *,
    state: ScanState,
    archive: str,
    path: str,
) -> None:
    _scan_chunk(raw, state=state, archive=archive, path=path, base_offset=0)
    config_section = config.get("config")
    if isinstance(config_section, dict):
        environment = config_section.get("Env")
        if isinstance(environment, list):
            for value in environment:
                if not isinstance(value, str) or "=" not in value:
                    continue
                key, assignment = value.split("=", 1)
                if re.fullmatch(SENSITIVE_KEY.decode("ascii"), key, re.IGNORECASE) and not _placeholder(assignment.encode()):
                    state.add("SECRET-ASSIGNMENT", archive, path, max(0, raw.find(value.encode())))
        labels = config_section.get("Labels")
        if isinstance(labels, dict):
            for key, value in labels.items():
                if (
                    isinstance(key, str)
                    and isinstance(value, str)
                    and re.fullmatch(SENSITIVE_KEY.decode("ascii"), key, re.IGNORECASE)
                    and not _placeholder(value.encode())
                ):
                    state.add("SECRET-ASSIGNMENT", archive, path, max(0, raw.find(value.encode())))
    history = config.get("history")
    if isinstance(history, list):
        for item in history:
            created_by = item.get("created_by") if isinstance(item, dict) else None
            if isinstance(created_by, str):
                encoded = created_by.encode("utf-8", errors="replace")
                _scan_assignments(
                    encoded,
                    state=state,
                    archive=archive,
                    path=path,
                    base_offset=max(0, raw.find(encoded)),
                    inline=True,
                )


def scan_software(
    path: Path,
    *,
    seed: Path,
    public_key: Path,
    cosign: Path,
    deadline: float,
) -> dict[str, object]:
    state = ScanState(deadline=deadline)
    digest = ""
    seed_digest = ""
    public_key_digest = ""
    try:
        with (
            _artifact_snapshot(path, deadline=deadline) as (snapshot, digest),
            _artifact_snapshot(seed, deadline=deadline) as (seed_snapshot, seed_digest),
            _artifact_snapshot(public_key, deadline=deadline) as (public_key_snapshot, public_key_digest),
        ):
            manifest = _verify_seed(
                seed=seed_snapshot,
                public_key=public_key_snapshot,
                cosign=cosign,
                host=False,
                deadline=deadline,
            )
            expected = {
                _local_image_reference(service, manifest["image_ids"][service]): manifest["image_ids"][service]
                for service in sorted(manifest["services"])
            }
            if _docker_archive_digests(snapshot) != expected:
                raise ValueError("Software Docker archive identity mismatch")
            with tarfile.open(snapshot, mode="r:*") as outer:
                manifest_file = outer.extractfile("manifest.json")
                docker_manifest = json.loads(manifest_file.read(4 * 1024 * 1024 + 1)) if manifest_file else None
                if not isinstance(docker_manifest, list) or not docker_manifest:
                    raise ValueError("Docker save manifest invalid")
                with tempfile.TemporaryDirectory(prefix="drycontroller-public-scan-software-") as temporary:
                    roots = Path(temporary)
                    for index, record in enumerate(docker_manifest):
                        if not isinstance(record, dict) or set(record) != {"Config", "RepoTags", "Layers"}:
                            raise ValueError("Docker save record invalid")
                        config_name = _safe_member_name(str(record["Config"]), state, "software")
                        tags = record["RepoTags"]
                        layers = record["Layers"]
                        if config_name is None or not isinstance(tags, list) or not tags or not isinstance(layers, list):
                            raise ValueError("Docker save record invalid")
                        if not all(isinstance(tag, str) and tag in expected for tag in tags):
                            raise ValueError("Docker save tag invalid")
                        config_file = outer.extractfile(config_name)
                        config_raw = config_file.read(4 * 1024 * 1024 + 1) if config_file else b""
                        config = json.loads(config_raw)
                        if not isinstance(config, dict) or len(config_raw) > 4 * 1024 * 1024:
                            raise ValueError("Docker image config invalid")
                        if any(expected[tag] != "sha256:" + hashlib.sha256(config_raw).hexdigest() for tag in tags):
                            raise ValueError("Docker image config digest mismatch")
                        label = f"software:{tags[0]}"
                        _scan_docker_config(config_raw, config, state=state, archive=label, path=config_name)
                        rootfs = roots / str(index)
                        rootfs.mkdir()
                        for layer_name in layers:
                            if not isinstance(layer_name, str):
                                raise ValueError("Docker layer name invalid")
                            safe_layer = _safe_member_name(layer_name, state, label)
                            if safe_layer is None:
                                raise ValueError("Docker layer path invalid")
                            _apply_and_scan_layer(
                                outer,
                                safe_layer,
                                rootfs=rootfs,
                                state=state,
                                archive=f"{label}:{safe_layer}",
                            )
                        _scan_tree(rootfs, state=state, archive=f"{label}:merged-rootfs")
    except ArtifactChangedError:
        state.add("SCAN-ARTIFACT-CHANGED", "software", path.name)
    except LimitExceeded as exc:
        state.add(exc.rule_id, exc.archive, exc.path)
    except (TimeoutError, subprocess.TimeoutExpired):
        state.add("SCAN-TIMEOUT", "software", path.name)
    except (HostReleaseError, OSError, ValueError, KeyError, json.JSONDecodeError, tarfile.TarError, subprocess.SubprocessError):
        state.add("SCAN-VERIFICATION-FAILED", "software", path.name)
    return _artifact_result(
        digest,
        state,
        seed_sha256=seed_digest,
        public_key_sha256=public_key_digest,
    )


def _preflight_iso(iso: Path, *, state: ScanState, archive: str) -> None:
    names = 0

    def consume_name(raw: bytes) -> None:
        nonlocal names
        state.check(archive, iso.name)
        try:
            name = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArchiveInspectionError("ISO member name is not UTF-8") from exc
        if name.strip() in {"", ".", "./"}:
            return
        if _safe_member_name(name.strip(), state, archive) is None:
            raise ArchiveInspectionError("ISO contains unsafe path")
        names += 1
        if names > MAX_ENTRIES:
            raise LimitExceeded("SCAN-RESOURCE-LIMIT", archive, name)

    _run_command_lines(
        ["bsdtar", "-tf", str(iso)],
        deadline=state.deadline,
        consume=consume_name,
    )

    metadata_entries = 0
    total = 0

    def consume_metadata(raw: bytes) -> None:
        nonlocal metadata_entries, total
        state.check(archive, iso.name)
        try:
            fields = raw.decode("utf-8").split(maxsplit=8)
            size = int(fields[4])
        except (UnicodeDecodeError, ValueError, IndexError) as exc:
            raise ArchiveInspectionError("ISO listing metadata invalid") from exc
        if fields[8].strip() in {".", "./"}:
            return
        if size < 0 or size > MAX_MEMBER_BYTES:
            raise LimitExceeded("SCAN-RESOURCE-LIMIT", archive, iso.name)
        metadata_entries += 1
        total += size
        if metadata_entries > MAX_ENTRIES or total > MAX_TOTAL_BYTES:
            raise LimitExceeded("SCAN-RESOURCE-LIMIT", archive, iso.name)

    _run_command_lines(
        ["bsdtar", "--numeric-owner", "-tvf", str(iso)],
        deadline=state.deadline,
        consume=consume_metadata,
    )
    if names == 0 or metadata_entries != names:
        raise ArchiveInspectionError("ISO listing entry count mismatch")


def _extract_iso(iso: Path, root: Path, *, state: ScanState, archive: str) -> None:
    state.check(archive, iso.name)
    _preflight_iso(iso, state=state, archive=archive)
    available = state.deadline - time.monotonic()
    if available <= 0:
        raise LimitExceeded("SCAN-TIMEOUT", archive, iso.name)
    timeout = max(1, int(available))
    completed = subprocess.run(
        ["bsdtar", "-xf", str(iso), "-C", str(root), "--no-same-owner", "--no-same-permissions"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise ValueError("ISO extraction failed")
    _preflight_tree(root, state=state, archive=archive)


def _tree_digests(root: Path, state: ScanState, archive: str) -> dict[str, str]:
    return {
        relative: sha256_file(path, deadline=state.deadline)
        for relative, path in _tree_files(root, state, archive, inspect=False)
    }


def scan_iso_surfaces(
    iso: Path,
    *,
    base_iso: Path | None,
    approved_base_iso_sha256: str | None,
    requested: set[str],
    deadline: float,
) -> dict[str, dict[str, object]]:
    states = {surface: ScanState(deadline=deadline) for surface in requested}
    iso_digest = ""
    base_digest: str | None = None
    try:
        with contextlib.ExitStack() as snapshots:
            iso_snapshot, iso_digest = snapshots.enter_context(
                _artifact_snapshot(iso, deadline=deadline)
            )
            base_snapshot: Path | None = None
            if "iso" in requested and base_iso is not None:
                base_snapshot, base_digest = snapshots.enter_context(
                    _artifact_snapshot(base_iso, deadline=deadline)
                )
                if (
                    approved_base_iso_sha256 is None
                    or re.fullmatch(r"[0-9a-f]{64}", approved_base_iso_sha256) is None
                    or approved_base_iso_sha256 not in APPROVED_BASE_ISO_SHA256S
                    or base_digest != approved_base_iso_sha256
                ):
                    raise ValueError("base ISO digest is not approved")
            with tempfile.TemporaryDirectory(prefix="drycontroller-public-scan-iso-") as temporary:
                directory = Path(temporary)
                final_root = directory / "final"
                final_root.mkdir()
                extraction_state = states.get("filesystem") or states["iso"]
                _extract_iso(iso_snapshot, final_root, state=extraction_state, archive="iso")
                only: set[str] | None = None
                if "iso" in requested and base_snapshot is not None:
                    base_root = directory / "base"
                    base_root.mkdir()
                    _extract_iso(base_snapshot, base_root, state=states["iso"], archive="base-iso")
                    final_digests = _tree_digests(final_root, states["iso"], "iso")
                    base_digests = _tree_digests(base_root, states["iso"], "base-iso")
                    only = {
                        path
                        for path, digest in final_digests.items()
                        if base_digests.get(path) != digest
                    }
                if "iso" in requested:
                    _scan_tree(final_root, state=states["iso"], archive="iso", only=only)
                if "filesystem" in requested:
                    _scan_tree(final_root, state=states["filesystem"], archive="filesystem")
    except ArtifactChangedError:
        for surface, state in states.items():
            state.add("SCAN-ARTIFACT-CHANGED", surface, iso.name)
    except LimitExceeded as exc:
        for state in states.values():
            state.add(exc.rule_id, exc.archive, exc.path)
    except (TimeoutError, subprocess.TimeoutExpired):
        for surface, state in states.items():
            state.add("SCAN-TIMEOUT", surface, iso.name)
    except (
        ArchiveInspectionError,
        OSError,
        UnicodeDecodeError,
        ValueError,
        subprocess.SubprocessError,
    ):
        for surface, state in states.items():
            state.add("SCAN-VERIFICATION-FAILED", surface, iso.name)
    results: dict[str, dict[str, object]] = {}
    for surface, state in states.items():
        if surface == "filesystem":
            result = _artifact_result(iso_digest, state)
            result["iso_sha256"] = result.pop("sha256")
            results[surface] = result
        else:
            extra = {"base_iso_sha256": base_digest} if base_digest is not None else {}
            results[surface] = _artifact_result(iso_digest, state, **extra)
    return results


def _validate_existing_report(report: object) -> dict[str, object]:
    if (
        not isinstance(report, dict)
        or set(report) != {"schema", "scanner_version", "policy_sha256", "passed", "artifacts"}
        or report.get("schema") != SCHEMA
        or report.get("scanner_version") != SCANNER_VERSION
        or report.get("policy_sha256") != POLICY_SHA256
        or not isinstance(report.get("artifacts"), dict)
    ):
        raise ValueError("existing scan report invalid")
    return report


def _write_report(path: Path, artifacts: dict[str, object]) -> dict[str, object]:
    report = {
        "schema": SCHEMA,
        "scanner_version": SCANNER_VERSION,
        "policy_sha256": POLICY_SHA256,
        "passed": bool(artifacts) and all(
            isinstance(value, dict) and value.get("passed") is True for value in artifacts.values()
        ),
        "artifacts": artifacts,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--surfaces", nargs="+", choices=SURFACES, required=True)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--append", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--host", type=Path)
    parser.add_argument("--host-seed", type=Path)
    parser.add_argument("--host-public-key", type=Path)
    parser.add_argument("--software", type=Path)
    parser.add_argument("--software-seed", type=Path)
    parser.add_argument("--software-public-key", type=Path)
    parser.add_argument("--iso", type=Path)
    parser.add_argument("--base-iso", type=Path)
    parser.add_argument("--approved-base-iso-sha256")
    parser.add_argument("--cosign", type=Path)
    args = parser.parse_args()
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be positive")
    requested = set(args.surfaces)
    if len(requested) != len(args.surfaces):
        parser.error("duplicate scan surface")
    required = {
        "source": (args.source,),
        "host": (args.host, args.host_seed, args.host_public_key),
        "software": (args.software, args.software_seed, args.software_public_key),
        "iso": (args.iso,),
        "filesystem": (args.iso,),
    }
    for surface in requested:
        if any(value is None or not value.is_file() for value in required[surface]):
            parser.error(f"{surface} scan inputs are missing")
    if args.base_iso is not None and "iso" in requested:
        if (
            args.approved_base_iso_sha256 is None
            or re.fullmatch(r"[0-9a-f]{64}", args.approved_base_iso_sha256) is None
        ):
            parser.error("base ISO diff scan requires --approved-base-iso-sha256")
    elif args.approved_base_iso_sha256 is not None:
        parser.error("--approved-base-iso-sha256 requires an ISO diff scan with --base-iso")
    cosign = args.cosign or (Path(shutil.which("cosign")) if shutil.which("cosign") else None)
    if requested.intersection({"host", "software"}) and (cosign is None or not cosign.is_file()):
        parser.error("host/software scans require --cosign or cosign on PATH")
    deadline = time.monotonic() + args.timeout_seconds
    artifacts: dict[str, object] = {}
    if args.append:
        try:
            artifacts.update(
                _validate_existing_report(json.loads(args.report.read_text(encoding="utf-8")))["artifacts"]
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            parser.error(str(exc))
    elif args.report.exists():
        parser.error("scan report already exists; use --append or remove it")
    try:
        if "source" in requested:
            artifacts["source"] = scan_source(args.source, deadline=deadline)
        if "host" in requested:
            assert cosign is not None
            artifacts["host"] = scan_host(
                args.host,
                seed=args.host_seed,
                public_key=args.host_public_key,
                cosign=cosign,
                deadline=deadline,
            )
        if "software" in requested:
            assert cosign is not None
            artifacts["software"] = scan_software(
                args.software,
                seed=args.software_seed,
                public_key=args.software_public_key,
                cosign=cosign,
                deadline=deadline,
            )
        if requested.intersection({"iso", "filesystem"}):
            artifacts.update(
                scan_iso_surfaces(
                    args.iso,
                    base_iso=args.base_iso,
                    approved_base_iso_sha256=args.approved_base_iso_sha256,
                    requested=requested.intersection({"iso", "filesystem"}),
                    deadline=deadline,
                )
            )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"public material scan could not inspect inputs: {type(exc).__name__}", file=sys.stderr)
        return 1
    report = _write_report(args.report, artifacts)
    for surface in sorted(requested):
        result = artifacts[surface]
        assert isinstance(result, dict)
        for finding in result.get("findings", []):
            print(
                "public material finding: "
                f"rule={finding['rule_id']} archive={finding['archive']} "
                f"path={finding['path']} offset={finding['offset']}",
                file=sys.stderr,
            )
    print("public material scan passed" if report["passed"] else "public material scan failed")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
