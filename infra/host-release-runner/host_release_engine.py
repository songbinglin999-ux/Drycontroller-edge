#!/usr/bin/python3
"""Immutable HostRelease runner with paired host/software rollback."""

from __future__ import annotations

import base64
import binascii
import contextlib
import datetime as dt
import fcntl
import hashlib
import hmac
import io
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator


HOST_RELEASE_SCHEMA = "drycontroller.host-release.v1"
HOST_ACTIVE_SCHEMA = "drycontroller.host-active.v1"
SOFTWARE_RELEASE_SCHEMA = "drycontroller.software-release.v1"
COORDINATOR_JOURNAL_SCHEMA = "drycontroller.host-release-coordinator.v1"
REGISTRY_AUTH_SCHEMA = "drycontroller.registry-auth.v2"
HOST_HEALTH_CONTRACT = "drycontroller.edge-health.v1"
COSIGN_TOOLCHAIN = {
    "version": "v3.1.3",
    "linux_amd64_sha256": "sha256:4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71",
}
COMMAND_TIMEOUT_SECONDS = 30 * 60
COMMAND_TERMINATE_GRACE_SECONDS = 10
COMMAND_OUTPUT_DRAIN_GRACE_SECONDS = 10
MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024
_HOST_MANIFEST_FIELDS = {
    "schema",
    "release_id",
    "sequence",
    "version",
    "source_commit",
    "registry",
    "signing_key_sha256",
    "cosign",
    "bundle",
    "topology",
    "topology_digest",
    "software_release_id",
    "software_manifest_digest",
    "files",
}
_SOFTWARE_MANIFEST_FIELDS = {
    "schema",
    "release_id",
    "host_release_id",
    "topology_digest",
    "sequence",
    "version",
    "source_commit",
    "registry",
    "signing_key_sha256",
    "cosign",
    "services",
    "image_ids",
}
_POLICY_FIELDS = {
    "state",
    "operation_id",
    "tenant_id",
    "line_id",
    "device_id",
    "node_name",
    "node_id",
    "host_release_id",
    "host_manifest_digest",
    "host_manifest_signature",
    "host_manifest",
    "software_release_id",
    "software_manifest_digest",
    "software_manifest_signature",
    "software_manifest",
}
_COORDINATOR_FIELDS = {
    "schema",
    "phase",
    "operation_id",
    "last_operation_id",
    "high_watermark",
    "active",
    "candidate",
    "previous",
    "effect",
    "error",
}
_OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_REGISTRY = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?$")
_REPOSITORY_PATH = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")
_SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_DOCKER = "/usr/bin/docker"
_DOCKER_COMPOSE = "/usr/libexec/docker/cli-plugins/docker-compose"
_BASH = "/usr/bin/bash"
_COSIGN = "/usr/local/bin/cosign"
_IDENTITY_ROOT = Path("/opt/drycontroller-identity")
_IDENTITY_SIGNER = "/opt/drycontroller-identity/venv/bin/python"
_IDENTITY_MODULE = "identity_service.host_authorization"
_SYSTEMCTL = "/usr/bin/systemctl"
_FRESH_INSTALL_GUARD = "/usr/libexec/drycontroller/assert-fresh-install-data-plane"
_SOFTWARE_HELPER_FILES = {
    "infra/ota-helper/drycontroller_ota_helper.py",
    "infra/ota-helper/release_engine.py",
    "infra/ota-helper/ota-apply",
    "infra/ota-helper/initialize-active-release",
    "infra/ota-helper/start-active-release",
}
_EXECUTABLE_PATHS = {
    "infra/mosquitto/entrypoint.sh",
    "infra/ota-helper/ota-apply",
    "infra/ota-helper/initialize-active-release",
    "infra/ota-helper/start-active-release",
}
_ALLOWED_PREFIXES = (
    "configs/",
    "infra/alertmanager/",
    "infra/mosquitto/",
    "infra/ota-helper/",
    "infra/postgres/",
    "infra/prometheus/",
)
_FORBIDDEN_COMPOSE_KEYS = {
    "build",
    "configs",
    "privileged",
    "pid",
    "ipc",
    "network_mode",
    "devices",
    "cap_add",
    "cap_drop",
    "security_opt",
    "secrets",
    "use_api_socket",
    "volumes_from",
}
_MAX_JSON_BYTES = 1024 * 1024
_MAX_LAYER_BYTES = 256 * 1024 * 1024
_REGISTRY_AUTH_SOURCE = Path("/var/lib/drycontroller-ota/registry-auth.json")
_DOCKER_CONFIG_PARENT = Path("/var/lib/drycontroller-ota")
_LOCAL_DOCKER_CONFIG = Path("/var/lib/drycontroller-ota/guard-docker-config/config.json")
_TOOL_IDENTITY_PATH = Path("/etc/drycontroller/runtime-tools.json")
_DAEMON_IDENTITY_PATH = Path("/etc/drycontroller/docker-daemon.json")
_DOCKER_SOCKET = Path("/run/docker.sock")
_TRUST_ROOT = Path("/")
_FIXED_ENV = {
    "HOME": "/root",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "DOCKER_HOST": "unix:///run/docker.sock",
    "COMPOSE_PROJECT_NAME": "drycontroller-edge",
}


class HostReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class HostReleasePaths:
    lock: Path
    journal: Path
    request: Path
    releases: Path
    current_link: Path
    compatibility_link: Path
    host_active: Path
    software_journal: Path
    software_releases: Path
    host_cosign_pubkey: Path
    software_cosign_pubkey: Path
    registry_status: Path
    device: Path
    cloud_config: Path
    runtime_env: Path

    @classmethod
    def production(cls) -> "HostReleasePaths":
        return cls(
            lock=Path("/var/lib/drycontroller-ota/release.lock"),
            journal=Path("/var/lib/drycontroller-host/coordinator-journal.json"),
            request=Path("/var/lib/drycontroller-host/request.json"),
            releases=Path("/opt/drycontroller-host/releases"),
            current_link=Path("/opt/drycontroller-host/current"),
            compatibility_link=Path("/opt/Drycontroller-edge"),
            host_active=Path("/var/lib/drycontroller-host/active.json"),
            software_journal=Path("/var/lib/drycontroller-ota/release-journal.json"),
            software_releases=Path("/var/lib/drycontroller-ota/releases"),
            host_cosign_pubkey=Path("/etc/drycontroller/keys/host-release-cosign.pub"),
            software_cosign_pubkey=Path("/etc/drycontroller/keys/cosign.pub"),
            registry_status=_REGISTRY_AUTH_SOURCE,
            device=Path("/var/lib/drycontroller/device.json"),
            cloud_config=Path("/etc/drycontroller/cloud-ops.env"),
            runtime_env=Path("/etc/drycontroller/runtime.env"),
        )


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_digest(value: dict[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def canonical_topology_digest(topology: dict[str, Any]) -> str:
    return canonical_digest(topology)


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise HostReleaseError(f"manifest {key} must be a non-empty string")
    return value


def _uuid(data: dict[str, Any], key: str) -> str:
    value = _text(data, key)
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise HostReleaseError(f"manifest {key} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise HostReleaseError(f"manifest {key} must be a canonical UUID")
    return value


def _validate_image(reference: object, *, registry: str) -> str:
    if not isinstance(reference, str) or "@" not in reference:
        raise HostReleaseError("image reference must be digest locked")
    repository, digest = reference.rsplit("@", 1)
    prefix = f"{registry}/"
    if not repository.startswith(prefix) or not _REPOSITORY_PATH.fullmatch(repository[len(prefix) :]):
        raise HostReleaseError("image reference must use the manifest registry")
    if not _DIGEST.fullmatch(digest):
        raise HostReleaseError("image reference must be digest locked")
    return reference


def _safe_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 240 or "\\" in value:
        raise HostReleaseError("host release file path invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise HostReleaseError("host release file path invalid")
    if value == "docker-compose.yml":
        return value
    if not value.startswith(_ALLOWED_PREFIXES):
        raise HostReleaseError("host release file path is outside the fixed allowlist")
    if value.startswith("configs/keys/"):
        raise HostReleaseError("host release may not contain trust roots")
    if value.startswith("infra/ota-helper/") and value not in _SOFTWARE_HELPER_FILES:
        raise HostReleaseError("host release OTA helper path is not fixed")
    return value


def validate_host_manifest(manifest: object) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != _HOST_MANIFEST_FIELDS:
        raise HostReleaseError("HostRelease manifest fields invalid")
    if manifest.get("schema") != HOST_RELEASE_SCHEMA:
        raise HostReleaseError("HostRelease schema invalid")
    _uuid(manifest, "release_id")
    _uuid(manifest, "software_release_id")
    sequence = manifest.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise HostReleaseError("HostRelease sequence invalid")
    if not _SEMVER.fullmatch(_text(manifest, "version")):
        raise HostReleaseError("HostRelease version invalid")
    if not _COMMIT.fullmatch(_text(manifest, "source_commit")):
        raise HostReleaseError("HostRelease source commit invalid")
    registry = _text(manifest, "registry")
    if not _REGISTRY.fullmatch(registry):
        raise HostReleaseError("HostRelease registry invalid")
    for key in ("signing_key_sha256", "topology_digest", "software_manifest_digest"):
        if not _DIGEST.fullmatch(_text(manifest, key)):
            raise HostReleaseError(f"HostRelease {key} invalid")
    if manifest.get("cosign") != COSIGN_TOOLCHAIN:
        raise HostReleaseError("HostRelease cosign toolchain invalid")
    bundle = manifest.get("bundle")
    if not isinstance(bundle, dict) or set(bundle) != {"image", "image_id"}:
        raise HostReleaseError("HostRelease bundle fields invalid")
    _validate_image(bundle.get("image"), registry=registry)
    if not isinstance(bundle.get("image_id"), str) or not _DIGEST.fullmatch(bundle["image_id"]):
        raise HostReleaseError("HostRelease bundle image ID invalid")
    topology = manifest.get("topology")
    if not isinstance(topology, dict) or set(topology) != {
        "compose_digest",
        "service_inventory",
        "health_contract",
    }:
        raise HostReleaseError("HostRelease topology fields invalid")
    if not isinstance(topology.get("compose_digest"), str) or not _DIGEST.fullmatch(topology["compose_digest"]):
        raise HostReleaseError("HostRelease Compose digest invalid")
    inventory = topology.get("service_inventory")
    if (
        not isinstance(inventory, list)
        or not inventory
        or len(inventory) > 128
        or inventory != sorted(set(inventory))
        or not all(isinstance(service, str) and _SERVICE_NAME.fullmatch(service) for service in inventory)
    ):
        raise HostReleaseError("HostRelease service inventory invalid")
    if topology.get("health_contract") != HOST_HEALTH_CONTRACT:
        raise HostReleaseError("HostRelease health contract invalid")
    if manifest["topology_digest"] != canonical_topology_digest(topology):
        raise HostReleaseError("HostRelease topology digest mismatch")
    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > 4096:
        raise HostReleaseError("HostRelease files invalid")
    seen: set[str] = set()
    compose: dict[str, Any] | None = None
    total_size = 0
    for record in files:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "size", "mode"}:
            raise HostReleaseError("HostRelease file fields invalid")
        path = _safe_path(record.get("path"))
        if path in seen:
            raise HostReleaseError("HostRelease file path duplicated")
        seen.add(path)
        if not isinstance(record.get("sha256"), str) or not _DIGEST.fullmatch(record["sha256"]):
            raise HostReleaseError("HostRelease file digest invalid")
        size = record.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > 64 * 1024 * 1024:
            raise HostReleaseError("HostRelease file size invalid")
        total_size += size
        if total_size > _MAX_LAYER_BYTES:
            raise HostReleaseError("HostRelease file set too large")
        expected_mode = 0o755 if path in _EXECUTABLE_PATHS else 0o644
        if record.get("mode") != expected_mode:
            raise HostReleaseError("HostRelease file mode invalid")
        if path == "docker-compose.yml":
            compose = record
    if compose is None or compose["sha256"] != topology["compose_digest"]:
        raise HostReleaseError("HostRelease Compose file binding invalid")
    if not _SOFTWARE_HELPER_FILES.issubset(seen):
        raise HostReleaseError("HostRelease fixed SoftwareRelease helper set incomplete")
    return manifest


def validate_software_manifest(manifest: object) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != _SOFTWARE_MANIFEST_FIELDS:
        raise HostReleaseError("SoftwareRelease manifest fields invalid")
    if manifest.get("schema") != SOFTWARE_RELEASE_SCHEMA:
        raise HostReleaseError("SoftwareRelease schema invalid")
    _uuid(manifest, "release_id")
    _uuid(manifest, "host_release_id")
    for key in ("topology_digest", "signing_key_sha256"):
        if not _DIGEST.fullmatch(_text(manifest, key)):
            raise HostReleaseError(f"SoftwareRelease {key} invalid")
    sequence = manifest.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise HostReleaseError("SoftwareRelease sequence invalid")
    if not _SEMVER.fullmatch(_text(manifest, "version")) or not _COMMIT.fullmatch(_text(manifest, "source_commit")):
        raise HostReleaseError("SoftwareRelease identity invalid")
    registry = _text(manifest, "registry")
    if not _REGISTRY.fullmatch(registry) or manifest.get("cosign") != COSIGN_TOOLCHAIN:
        raise HostReleaseError("SoftwareRelease registry or cosign invalid")
    services = manifest.get("services")
    image_ids = manifest.get("image_ids")
    if not isinstance(services, dict) or not services or not isinstance(image_ids, dict) or set(image_ids) != set(services):
        raise HostReleaseError("SoftwareRelease service set invalid")
    for service, reference in services.items():
        if not isinstance(service, str) or not _SERVICE_NAME.fullmatch(service):
            raise HostReleaseError("SoftwareRelease service name invalid")
        _validate_image(reference, registry=registry)
        if not isinstance(image_ids[service], str) or not _DIGEST.fullmatch(image_ids[service]):
            raise HostReleaseError("SoftwareRelease image ID invalid")
    return manifest


def assert_pair(host: dict[str, Any], software: dict[str, Any]) -> None:
    if (
        host["software_release_id"] != software["release_id"]
        or host["software_manifest_digest"] != canonical_digest(software)
        or software["host_release_id"] != host["release_id"]
        or software["topology_digest"] != host["topology_digest"]
        or sorted(software["services"]) != host["topology"]["service_inventory"]
        or software["signing_key_sha256"] == host["signing_key_sha256"]
    ):
        raise HostReleaseError("HostRelease and SoftwareRelease pair mismatch")


def _read_json(path: Path) -> Any:
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            raise HostReleaseError(f"JSON file too large: {path.name}")
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HostReleaseError(f"required file missing: {path.name}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostReleaseError(f"invalid JSON file: {path.name}") from exc


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _docker_archive_digests(path: Path) -> dict[str, str]:
    try:
        with tarfile.open(path, "r:*") as archive:
            manifest_file = archive.extractfile("manifest.json")
            manifest = json.loads(manifest_file.read(_MAX_JSON_BYTES + 1)) if manifest_file else None
            if not isinstance(manifest, list) or not manifest:
                raise HostReleaseError("Docker image archive manifest invalid")
            result: dict[str, str] = {}
            for item in manifest:
                if not isinstance(item, dict):
                    raise HostReleaseError("Docker image archive entry invalid")
                config_name = item.get("Config")
                tags = item.get("RepoTags")
                if not isinstance(config_name, str) or not isinstance(tags, list):
                    raise HostReleaseError("Docker image archive entry invalid")
                config_file = archive.extractfile(config_name)
                config = config_file.read(_MAX_JSON_BYTES + 1) if config_file else b""
                if not config or len(config) > _MAX_JSON_BYTES or not isinstance(json.loads(config), dict):
                    raise HostReleaseError("Docker image archive config invalid")
                digest = f"sha256:{hashlib.sha256(config).hexdigest()}"
                for tag in tags:
                    if not isinstance(tag, str) or (tag in result and result[tag] != digest):
                        raise HostReleaseError("Docker image archive tag invalid")
                    result[tag] = digest
            return result
    except HostReleaseError:
        raise
    except (OSError, tarfile.TarError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostReleaseError("Docker image archive invalid") from exc


def _local_image_reference(service: str, image_id: str) -> str:
    if not _SERVICE_NAME.fullmatch(service) or not _DIGEST.fullmatch(image_id):
        raise HostReleaseError("SoftwareRelease local image identity invalid")
    return f"drycontroller.local/{service}:sha256-{image_id.removeprefix('sha256:')}"


def _validate_ancestor_chain(path: Path) -> None:
    root = _TRUST_ROOT
    if not path.is_absolute() or (path != root and root not in path.parents):
        raise OSError("path outside trusted root")
    current = path
    while True:
        metadata = current.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
        ):
            raise OSError("untrusted ancestor")
        if current == root:
            return
        current = current.parent


def _read_identity_file(path: Path, schema: str) -> dict[str, Any]:
    descriptor = -1
    try:
        _validate_ancestor_chain(path.parent)
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or metadata.st_nlink != 1
        ):
            raise OSError("identity metadata invalid")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        raw = os.read(descriptor, 64 * 1024 + 1)
        if (
            (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or not raw
            or len(raw) > 64 * 1024
        ):
            raise OSError("identity changed")
        payload = json.loads(raw, object_pairs_hook=_strict_json_object)
    except (OSError, UnicodeError, ValueError) as exc:
        raise HostReleaseError("runtime identity unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(payload, dict) or payload.get("schema") != schema:
        raise HostReleaseError("runtime identity invalid")
    return payload


def _open_trusted_executable(value: str, label: str) -> int:
    path = Path(value)
    descriptor = -1
    try:
        _validate_ancestor_chain(path.parent)
        metadata = path.lstat()
        if (
            not path.is_absolute()
            or path.resolve(strict=True) != path
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
            or not metadata.st_mode & 0o111
            or metadata.st_nlink != 1
        ):
            raise OSError("executable metadata invalid")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise OSError("executable changed")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise HostReleaseError(f"fixed {label} executable unavailable") from exc
    try:
        identity = _read_identity_file(_TOOL_IDENTITY_PATH, "drycontroller.runtime-tools.v1")
    except HostReleaseError:
        os.close(descriptor)
        raise
    executables = identity.get("executables")
    record = executables.get(value) if isinstance(executables, dict) else None
    if (
        not isinstance(executables, dict)
        or set(executables) != {_DOCKER, _DOCKER_COMPOSE, _BASH}
        or not isinstance(record, dict)
        or set(record) != {"sha256"}
        or record["sha256"] != f"sha256:{digest.hexdigest()}"
    ):
        os.close(descriptor)
        raise HostReleaseError(f"fixed {label} executable is untrusted")
    return descriptor


def _validate_trusted_executable(value: str, label: str) -> None:
    os.close(_open_trusted_executable(value, label))


def _descriptor_path(descriptor: int) -> str:
    prefix = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
    return f"{prefix}/{descriptor}"


def _open_trusted_program(value: str, label: str) -> int:
    path = Path(value)
    descriptor = -1
    try:
        _validate_ancestor_chain(path.parent)
        metadata = path.lstat()
        if (
            path.resolve(strict=True) != path
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
            or not metadata.st_mode & 0o111
            or metadata.st_nlink != 1
        ):
            raise OSError("program metadata invalid")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise OSError("program changed")
        return descriptor
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise HostReleaseError(f"fixed {label} is untrusted") from exc


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_registry_auth_document(path: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    path = path or _REGISTRY_AUTH_SOURCE
    descriptor = -1
    try:
        metadata = path.lstat()
        parent_path = path.parent
        parent = parent_path.lstat()
        resolved = path.resolve(strict=True)
        if (
            not path.is_absolute()
            or resolved != path
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or metadata.st_nlink != 1
            or parent_path.resolve(strict=True) != parent_path
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise OSError("registry auth source metadata invalid")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise OSError("registry auth source changed")
        raw = os.read(descriptor, _MAX_JSON_BYTES + 1)
        if not raw or len(raw) > _MAX_JSON_BYTES:
            raise OSError("registry auth source size invalid")
        payload = json.loads(raw, object_pairs_hook=_strict_json_object)
    except (OSError, UnicodeError, ValueError) as exc:
        raise HostReleaseError("root registry auth source invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "auths", "credential"}
        or payload.get("schema") != REGISTRY_AUTH_SCHEMA
        or not isinstance(payload["auths"], dict)
    ):
        raise HostReleaseError("root registry auth source schema invalid")
    normalized: dict[str, dict[str, str]] = {}
    for registry, entry in payload["auths"].items():
        if not isinstance(registry, str) or _REGISTRY.fullmatch(registry) is None:
            raise HostReleaseError("root registry auth source schema invalid")
        if not isinstance(entry, dict) or set(entry) != {"auth"} or not isinstance(entry["auth"], str):
            raise HostReleaseError("root registry auth source schema invalid")
        try:
            decoded = base64.b64decode(entry["auth"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HostReleaseError("root registry auth source schema invalid") from exc
        username, separator, secret = decoded.partition(b":")
        if not username or separator != b":" or not secret or len(decoded) > 4096:
            raise HostReleaseError("root registry auth source schema invalid")
        normalized[registry] = {"auth": entry["auth"]}
    credential = payload.get("credential")
    if (
        not isinstance(credential, dict)
        or set(credential) != {
            "status",
            "credential_id",
            "registry_host",
            "username",
            "secret_fingerprint",
            "version",
            "transaction_id",
            "audit_id",
            "updated_at",
        }
        or credential.get("status") != "active"
        or not isinstance(credential.get("credential_id"), str)
        or not credential["credential_id"]
        or not isinstance(credential.get("registry_host"), str)
        or credential["registry_host"] not in normalized
        or not isinstance(credential.get("username"), str)
        or not credential["username"]
        or not isinstance(credential.get("secret_fingerprint"), str)
        or not credential["secret_fingerprint"]
        or isinstance(credential.get("version"), bool)
        or not isinstance(credential.get("version"), int)
        or credential["version"] < 1
        or not isinstance(credential.get("transaction_id"), str)
        or _OPERATION_ID.fullmatch(credential["transaction_id"]) is None
        or isinstance(credential.get("audit_id"), bool)
        or not isinstance(credential.get("audit_id"), int)
        or credential["audit_id"] < 1
        or not isinstance(credential.get("updated_at"), str)
        or not credential["updated_at"]
        or len(normalized) != 1
    ):
        raise HostReleaseError("root registry auth source schema invalid")
    try:
        bound_username = base64.b64decode(
            normalized[credential["registry_host"]]["auth"], validate=True
        ).partition(b":")[0].decode("utf-8")
    except (UnicodeDecodeError, binascii.Error, ValueError) as exc:
        raise HostReleaseError("root registry auth source schema invalid") from exc
    if bound_username != credential["username"]:
        raise HostReleaseError("root registry auth source schema invalid")
    return {"auths": normalized}, dict(credential)


def _read_registry_auth_source() -> dict[str, Any]:
    return _read_registry_auth_document()[0]


def _validate_local_docker_config() -> None:
    path = _LOCAL_DOCKER_CONFIG
    descriptor = -1
    try:
        metadata = path.lstat()
        parent_path = path.parent
        parent = parent_path.lstat()
        if (
            path.resolve(strict=True) != path
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or metadata.st_nlink != 1
            or parent_path.resolve(strict=True) != parent_path
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o500
        ):
            raise OSError("local Docker config metadata invalid")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        raw = os.read(descriptor, 4097)
        if (
            (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or len(raw) > 4096
            or json.loads(raw, object_pairs_hook=_strict_json_object) != {"auths": {}}
        ):
            raise OSError("local Docker config invalid")
    except (OSError, UnicodeError, ValueError) as exc:
        raise HostReleaseError("local Docker config invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _docker_environment(*, registry_network: bool) -> contextlib.AbstractContextManager[dict[str, str]]:
    if registry_network:
        return _private_docker_environment()
    _validate_local_docker_config()
    return contextlib.nullcontext(
        {**_FIXED_ENV, "DOCKER_CONFIG": str(_LOCAL_DOCKER_CONFIG.parent)}
    )


def _requires_registry_auth(args: list[str]) -> bool:
    return bool(
        args
        and (
            (args[0] == _DOCKER and len(args) > 1 and args[1] == "pull")
            or (args[0] == _COSIGN and len(args) > 1 and args[1] == "verify")
        )
    )


@contextlib.contextmanager
def _opened_docker_socket() -> Iterator[int]:
    descriptor = -1
    try:
        _validate_ancestor_chain(_DOCKER_SOCKET.parent)
        metadata = _DOCKER_SOCKET.lstat()
        if not hasattr(os, "O_PATH") or not Path("/proc/self/fd").is_dir():
            raise OSError("descriptor-bound Unix sockets require Linux procfs")
        descriptor = os.open(
            _DOCKER_SOCKET,
            os.O_PATH | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise HostReleaseError("Docker socket unavailable") from exc
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o002
        or metadata.st_nlink != 1
        or not stat.S_ISSOCK(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
    ):
        os.close(descriptor)
        raise HostReleaseError("Docker socket is untrusted")
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _docker_daemon_identity(docker_host: str, socket_descriptor: int) -> dict[str, str]:
    expected = _read_identity_file(
        _DAEMON_IDENTITY_PATH,
        "drycontroller.docker-daemon.v1",
    )
    if set(expected) != {"schema", "id", "name", "root_dir"} or not all(
        isinstance(expected.get(key), str) and expected[key]
        for key in ("id", "name", "root_dir")
    ):
        raise HostReleaseError("Docker daemon identity invalid")
    _validate_local_docker_config()
    descriptor = _open_trusted_executable(_DOCKER, "Docker")
    try:
        completed = subprocess.run(
            [
                _descriptor_path(descriptor),
                "info",
                "--format",
                "{{.ID}}\\n{{.Name}}\\n{{.DockerRootDir}}",
            ],
            env={
                **_FIXED_ENV,
                "DOCKER_HOST": docker_host,
                "DOCKER_CONFIG": str(_LOCAL_DOCKER_CONFIG.parent),
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=15,
            pass_fds=(descriptor, socket_descriptor),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HostReleaseError("Docker daemon identity unavailable") from exc
    finally:
        os.close(descriptor)
    lines = completed.stdout.splitlines()
    observed = dict(zip(("id", "name", "root_dir"), lines, strict=False))
    if (
        completed.returncode != 0
        or len(completed.stdout.encode("utf-8")) > 4096
        or len(lines) != 3
        or observed != {key: expected[key] for key in ("id", "name", "root_dir")}
    ):
        raise HostReleaseError("Docker daemon identity mismatch")
    return observed


@contextlib.contextmanager
def _bound_docker_daemon() -> Iterator[tuple[str, int]]:
    with _opened_docker_socket() as socket_descriptor:
        docker_host = f"unix:///proc/self/fd/{socket_descriptor}"
        daemon_identity = _docker_daemon_identity(docker_host, socket_descriptor)
        try:
            yield docker_host, socket_descriptor
        finally:
            if _docker_daemon_identity(docker_host, socket_descriptor) != daemon_identity:
                raise HostReleaseError("Docker daemon identity changed")


@contextlib.contextmanager
def _opened_command(args: list[str]) -> Iterator[tuple[list[str], tuple[int, ...]]]:
    descriptors: list[int] = []
    command = list(args)
    try:
        if args and args[0] in {_DOCKER, _DOCKER_COMPOSE, _BASH}:
            label = {
                _DOCKER: "Docker",
                _DOCKER_COMPOSE: "Docker Compose",
                _BASH: "Bash",
            }[args[0]]
            descriptor = _open_trusted_executable(args[0], label)
            descriptors.append(descriptor)
            command[0] = _descriptor_path(descriptor)
        if len(args) > 1 and args[0] == _BASH and args[1] == _FRESH_INSTALL_GUARD:
            descriptor = _open_trusted_program(_FRESH_INSTALL_GUARD, "fresh-install guard")
            descriptors.append(descriptor)
            command[1] = _descriptor_path(descriptor)
        yield command, tuple(descriptors)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


@contextlib.contextmanager
def _private_docker_environment() -> Iterator[dict[str, str]]:
    payload = _read_registry_auth_source()
    try:
        with tempfile.TemporaryDirectory(prefix=".docker-config-", dir=_DOCKER_CONFIG_PARENT) as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            config = directory / "config.json"
            descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            try:
                os.write(descriptor, canonical_json_bytes(payload))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            yield {**_FIXED_ENV, "DOCKER_CONFIG": str(directory)}
    except OSError as exc:
        raise HostReleaseError("private Docker auth config unavailable") from exc


class HostReleaseEngine:
    def __init__(
        self,
        *,
        paths: HostReleasePaths | None = None,
        command_runner: Callable[[list[str], Path | None, bytes | None], str] | None = None,
        policy_fetcher: Callable[[str], dict[str, Any]] | None = None,
        identity_authorizer: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        self.paths = paths or HostReleasePaths.production()
        self._command_runner = command_runner or self._run_command
        self._policy_fetcher = policy_fetcher or self._fetch_authorized_policy
        self._identity_authorizer = identity_authorizer or self._authorize_with_tpm

    @contextlib.contextmanager
    def _transaction_lock(self) -> Iterator[int]:
        try:
            self.paths.lock.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.paths.lock,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as exc:
            raise HostReleaseError("release transaction lock unavailable") from exc
        try:
            yield descriptor
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @staticmethod
    def _command_group_exists(group_id: int) -> bool:
        try:
            os.killpg(group_id, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    @staticmethod
    def _terminate_command(process: subprocess.Popen[bytes]) -> None:
        group_id = process.pid
        try:
            os.killpg(group_id, signal.SIGTERM)
        except ProcessLookupError:
            if process.poll() is None:
                process.wait()
            return
        except OSError:
            process.terminate()

        deadline = time.monotonic() + COMMAND_TERMINATE_GRACE_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is None:
                try:
                    process.wait(timeout=min(0.05, max(0.01, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
            if not HostReleaseEngine._command_group_exists(group_id):
                return
            time.sleep(0.01)

        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            if process.poll() is None:
                process.kill()
        if process.poll() is None:
            process.wait()

    @staticmethod
    def _read_command_output(
        process: subprocess.Popen[bytes],
        output: bytearray,
        too_large: threading.Event,
        errors: list[Exception],
    ) -> None:
        assert process.stdout is not None
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                remaining = max(0, MAX_COMMAND_OUTPUT_BYTES - len(output))
                output.extend(chunk[:remaining])
                if len(chunk) > remaining and not too_large.is_set():
                    too_large.set()
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass
        except (OSError, ValueError) as exc:
            errors.append(exc)
        finally:
            process.stdout.close()

    @classmethod
    def _finish_command_output_reader(cls, process: subprocess.Popen[bytes], reader: threading.Thread) -> bool:
        reader.join(timeout=COMMAND_OUTPUT_DRAIN_GRACE_SECONDS)
        if not reader.is_alive():
            return True
        cls._terminate_command(process)
        reader.join(timeout=COMMAND_OUTPUT_DRAIN_GRACE_SECONDS)
        return not reader.is_alive()

    def _run_command(self, args: list[str], cwd: Path | None = None, input_bytes: bytes | None = None) -> str:
        uses_docker = False
        if args and args[0] == _DOCKER:
            uses_docker = True
        elif args and args[0] == _DOCKER_COMPOSE:
            uses_docker = True
        elif args and args[0] == _BASH and len(args) > 1 and args[1] == _FRESH_INSTALL_GUARD:
            pass
        elif args and Path(args[0]).name in {"docker", "docker-compose"}:
            raise HostReleaseError("Docker executable path drift")
        inherited_fds: tuple[int, ...] = ()
        inherits_release_lock = (
            len(args) == 3
            and args[0].endswith("/start-active-release")
            and args[1] == "--lock-fd"
        ) or (
            len(args) == 6
            and args[0] == _BASH
            and args[1] == _FRESH_INSTALL_GUARD
            and args[2] == "--project"
            and args[3] == _FIXED_ENV["COMPOSE_PROJECT_NAME"]
            and args[4] == "--lock-fd"
        )
        if inherits_release_lock:
            try:
                inherited_fds = (int(args[-1]),)
            except ValueError as exc:
                raise HostReleaseError("inherited release lock descriptor invalid") from exc
        process: subprocess.Popen[bytes] | None = None
        input_handle: Any = subprocess.DEVNULL
        output = bytearray()
        output_too_large = threading.Event()
        reader_errors: list[Exception] = []
        reader: threading.Thread | None = None
        timed_out = False
        command_succeeded = False
        resources = contextlib.ExitStack()
        try:
            command_env = (
                resources.enter_context(
                    _docker_environment(registry_network=_requires_registry_auth(args))
                )
                if uses_docker or _requires_registry_auth(args)
                else _FIXED_ENV
            )
            socket_fds: tuple[int, ...] = ()
            if uses_docker:
                docker_host, socket_descriptor = resources.enter_context(_bound_docker_daemon())
                command_env = {**command_env, "DOCKER_HOST": docker_host}
                socket_fds = (socket_descriptor,)
            command_args, executable_fds = resources.enter_context(_opened_command(args))
            passed_fds = tuple(dict.fromkeys((*inherited_fds, *executable_fds, *socket_fds)))
            if input_bytes is not None:
                input_handle = tempfile.TemporaryFile()
                input_handle.write(input_bytes)
                input_handle.seek(0)
            process = subprocess.Popen(
                command_args,
                cwd=cwd,
                env=command_env,
                stdin=input_handle,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                pass_fds=passed_fds,
                close_fds=True,
                start_new_session=True,
            )
            reader = threading.Thread(
                target=self._read_command_output,
                args=(process, output, output_too_large, reader_errors),
                daemon=True,
            )
            try:
                reader.start()
            except RuntimeError as exc:
                raise HostReleaseError(f"command output unavailable: {Path(args[0]).name}") from exc
            try:
                return_code = process.wait(timeout=COMMAND_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_command(process)
                return_code = process.returncode
            if not self._finish_command_output_reader(process, reader):
                raise HostReleaseError(f"command output unavailable: {Path(args[0]).name}")
            if timed_out:
                raise HostReleaseError(f"command unavailable: {Path(args[0]).name}")
            if reader_errors:
                raise HostReleaseError(f"command output unavailable: {Path(args[0]).name}")
            if output_too_large.is_set():
                raise HostReleaseError(f"command output too large: {Path(args[0]).name}")
            if return_code != 0:
                raise HostReleaseError(f"command failed: {Path(args[0]).name}")
            try:
                decoded = output.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HostReleaseError("command output invalid") from exc
            command_succeeded = True
            return decoded
        except OSError as exc:
            raise HostReleaseError(f"command unavailable: {Path(args[0]).name}") from exc
        finally:
            if process is not None and not command_succeeded:
                self._terminate_command(process)
                if process.stdout is not None and not process.stdout.closed:
                    process.stdout.close()
            if input_handle != subprocess.DEVNULL:
                input_handle.close()
            resources.close()

    def _cloud_credentials(self) -> tuple[str, str]:
        values: dict[str, str] = {}
        try:
            lines = self.paths.cloud_config.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise HostReleaseError("Cloud configuration unavailable") from exc
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            if key != "CLOUD_OPS_BASE_URL":
                continue
            try:
                tokens = shlex.split(raw_value, posix=True)
            except ValueError as exc:
                raise HostReleaseError("Cloud configuration invalid") from exc
            if len(tokens) != 1 or not tokens[0]:
                raise HostReleaseError("Cloud configuration invalid")
            values[key] = tokens[0]
        if set(values) != {"CLOUD_OPS_BASE_URL"} or not values["CLOUD_OPS_BASE_URL"].startswith("https://"):
            raise HostReleaseError("Cloud configuration incomplete")
        identity = _read_json(self.paths.device)
        if not isinstance(identity, dict) or identity.get("schema") != "drycontroller.device-identity.v1":
            raise HostReleaseError("device identity invalid")
        credential = identity.get("runtime_credential")
        credential_hash = identity.get("runtime_credential_hash")
        if (
            not isinstance(credential, str)
            or not credential
            or not isinstance(credential_hash, str)
            or not hmac.compare_digest(hashlib.sha256(credential.encode()).hexdigest(), credential_hash)
        ):
            raise HostReleaseError("device identity invalid")
        return values["CLOUD_OPS_BASE_URL"].rstrip("/"), credential

    def _request_json(
        self,
        url: str,
        *,
        credential: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = canonical_json_bytes(payload) if payload is not None else None
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "X-Device-Credential": credential,
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read(_MAX_JSON_BYTES + 1)
        except (OSError, urllib.error.URLError) as exc:
            raise HostReleaseError("HostRelease policy fetch failed") from exc
        if len(raw) > _MAX_JSON_BYTES:
            raise HostReleaseError("HostRelease policy too large")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HostReleaseError("HostRelease policy invalid") from exc
        if not isinstance(value, dict):
            raise HostReleaseError("HostRelease policy invalid")
        return value

    def _authorize_with_tpm(self, challenge: dict[str, Any]) -> str:
        result = self._command_runner(
            [_IDENTITY_SIGNER, "-m", _IDENTITY_MODULE],
            _IDENTITY_ROOT,
            canonical_json_bytes(challenge),
        )
        try:
            payload = json.loads(result)
        except json.JSONDecodeError as exc:
            raise HostReleaseError("TPM authorization response invalid") from exc
        signature = payload.get("signature") if isinstance(payload, dict) else None
        if payload.get("challenge_id") != challenge.get("challenge_id") or not isinstance(signature, str):
            raise HostReleaseError("TPM authorization response invalid")
        return signature

    def _fetch_authorized_policy(self, operation_id: str) -> dict[str, Any]:
        base_url, credential = self._cloud_credentials()
        path = f"{base_url}/api/v1/host-releases/deployment-policy/{operation_id}"
        policy = self._request_json(path, credential=credential)
        if policy.get("state") == "challenge":
            challenge = {
                "schema": "drycontroller.device-identity-challenge.v1",
                "domain": "drycontroller.device-identity",
                "operation": "host_release",
                "device_id": policy.get("device_id"),
                "challenge_id": policy.get("challenge_id"),
                "challenge_nonce": policy.get("challenge_nonce"),
                "request_id": policy.get("request_id"),
                "identity_fingerprint": policy.get("identity_fingerprint"),
                "runtime_credential_hash": policy.get("runtime_credential_hash"),
                "tenant_id": policy.get("tenant_id"),
                "line_id": policy.get("line_id"),
                "node_name": policy.get("node_name"),
                "node_id": policy.get("node_id"),
                "job_id": policy.get("job_id"),
                "job_item_id": policy.get("job_item_id"),
                "host_release_id": policy.get("host_release_id"),
                "host_manifest_digest": policy.get("host_manifest_digest"),
                "software_release_id": policy.get("software_release_id"),
                "software_manifest_digest": policy.get("software_manifest_digest"),
                "expires_at": policy.get("challenge_expires_at"),
            }
            signature = self._identity_authorizer(challenge)
            policy = self._request_json(
                f"{path}/authorize",
                credential=credential,
                payload={"signature": signature},
            )
        return policy

    def _read_operation_id(self) -> str:
        request = _read_json(self.paths.request)
        operation_id = request.get("operation_id") if isinstance(request, dict) and set(request) == {"operation_id"} else None
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
            raise HostReleaseError("HostRelease operation ID invalid")
        return operation_id

    def _load_journal(self) -> dict[str, Any]:
        journal = _read_json(self.paths.journal)
        if (
            not isinstance(journal, dict)
            or set(journal) != _COORDINATOR_FIELDS
            or journal.get("schema") != COORDINATOR_JOURNAL_SCHEMA
            or not isinstance(journal.get("phase"), str)
            or not isinstance(journal.get("high_watermark"), int)
        ):
            raise HostReleaseError("HostRelease coordinator journal invalid")
        return journal

    def _persist(self, journal: dict[str, Any]) -> None:
        _atomic_write(self.paths.journal, canonical_json_bytes(journal))

    def _effect(self, journal: dict[str, Any], name: str, action: Callable[[], Any]) -> Any:
        journal["effect"] = {"name": name, "status": "started"}
        self._persist(journal)
        try:
            result = action()
        except Exception as exc:  # noqa: BLE001 - persist any root effect failure
            journal["effect"] = {"name": name, "status": "failed"}
            journal["error"] = str(exc)[:512]
            self._persist(journal)
            if isinstance(exc, HostReleaseError):
                raise
            raise HostReleaseError(f"HostRelease effect failed: {name}") from exc
        journal["effect"] = {"name": name, "status": "succeeded"}
        self._persist(journal)
        return result

    def _validate_policy(self, policy: object, operation_id: str) -> dict[str, Any]:
        if not isinstance(policy, dict) or set(policy) != _POLICY_FIELDS or policy.get("state") != "authorized":
            raise HostReleaseError("HostRelease policy fields invalid")
        if policy.get("operation_id") != operation_id or policy.get("node_id") != int(operation_id[16:], 16):
            raise HostReleaseError("HostRelease policy operation binding mismatch")
        identity = _read_json(self.paths.device)
        for key in ("tenant_id", "line_id", "device_id", "node_name"):
            if policy.get(key) != identity.get(key):
                raise HostReleaseError("HostRelease policy device binding mismatch")
        host = validate_host_manifest(policy.get("host_manifest"))
        software = validate_software_manifest(policy.get("software_manifest"))
        assert_pair(host, software)
        if host["registry"] != software["registry"]:
            raise HostReleaseError("HostRelease and SoftwareRelease registry mismatch")
        _, registry_status = _read_registry_auth_document(self.paths.registry_status)
        if (
            not isinstance(registry_status, dict)
            or registry_status.get("status") != "active"
            or registry_status.get("registry_host") != host["registry"]
        ):
            raise HostReleaseError("HostRelease registry binding invalid")
        if (
            policy.get("host_release_id") != host["release_id"]
            or policy.get("host_manifest_digest") != canonical_digest(host)
            or policy.get("software_release_id") != software["release_id"]
            or policy.get("software_manifest_digest") != canonical_digest(software)
            or not isinstance(policy.get("host_manifest_signature"), str)
            or not isinstance(policy.get("software_manifest_signature"), str)
        ):
            raise HostReleaseError("HostRelease policy manifest binding mismatch")
        return policy

    def _verify_key(self, path: Path, expected_digest: str) -> None:
        try:
            metadata = path.lstat()
            raw = path.read_bytes()
        except OSError as exc:
            raise HostReleaseError("release signing key unavailable") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_mode & 0o022
            or not raw
            or len(raw) > 64 * 1024
        ):
            raise HostReleaseError("release signing key file invalid")
        if f"sha256:{hashlib.sha256(raw).hexdigest()}" != expected_digest:
            raise HostReleaseError("release signing key digest mismatch")

    def _verify_blob_signature(
        self,
        *,
        manifest: dict[str, Any],
        signature: str,
        public_key: Path,
        prefix: str,
    ) -> None:
        try:
            signature_bytes = base64.b64decode(signature, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HostReleaseError("release signature encoding invalid") from exc
        if not signature_bytes or len(signature_bytes) > 16 * 1024:
            raise HostReleaseError("release signature size invalid")
        with tempfile.TemporaryDirectory(prefix="drycontroller-host-signature-") as temporary:
            directory = Path(temporary)
            manifest_path = directory / f"{prefix}-manifest.json"
            signature_path = directory / f"{prefix}-manifest.sigstore.json"
            _atomic_write(manifest_path, canonical_json_bytes(manifest))
            _atomic_write(signature_path, signature_bytes)
            self._command_runner(
                [
                    _COSIGN,
                    "verify-blob",
                    "--key",
                    str(public_key),
                    "--insecure-ignore-tlog=true",
                    "--bundle",
                    str(signature_path),
                    str(manifest_path),
                ],
                None,
                None,
            )

    def _release_dir(self, host: dict[str, Any]) -> Path:
        return self.paths.releases / host["release_id"]

    def _staging_dir(self, host: dict[str, Any]) -> Path:
        return self.paths.releases / f".{host['release_id']}.staging"

    def _pull_and_export_bundle(self, host: dict[str, Any], staging: Path) -> Path:
        bundle = host["bundle"]
        reference = bundle["image"]
        self._command_runner([_DOCKER, "pull", reference], None, None)
        self._command_runner(
            [
                _COSIGN,
                "verify",
                "--insecure-ignore-tlog=true",
                "--key",
                str(self.paths.host_cosign_pubkey),
                reference,
            ],
            None,
            None,
        )
        actual_id = self._command_runner(
            [_DOCKER, "image", "inspect", "--format", "{{.Id}}", reference],
            None,
            None,
        ).strip()
        if not _DIGEST.fullmatch(actual_id):
            raise HostReleaseError("HostRelease bundle runtime image ID invalid")
        archive = staging / "bundle-image.tar"
        self._command_runner([_DOCKER, "image", "save", "-o", str(archive), reference], None, None)
        return archive

    def _extract_bundle(self, archive: Path, host: dict[str, Any], staging: Path) -> None:
        rootfs = staging / "rootfs"
        rootfs.mkdir(mode=0o700)
        try:
            with tarfile.open(archive, "r:*") as outer:
                manifest_member = outer.getmember("manifest.json")
                if not manifest_member.isfile() or manifest_member.size > _MAX_JSON_BYTES:
                    raise HostReleaseError("OCI image manifest invalid")
                manifest_raw = outer.extractfile(manifest_member)
                image_manifest = json.loads(manifest_raw.read()) if manifest_raw is not None else None
                if not isinstance(image_manifest, list) or len(image_manifest) != 1:
                    raise HostReleaseError("HostRelease bundle must contain one image")
                image_record = image_manifest[0]
                if not isinstance(image_record, dict) or set(image_record) != {"Config", "RepoTags", "Layers"}:
                    raise HostReleaseError("OCI image manifest fields invalid")
                config_name = image_record.get("Config")
                if not isinstance(config_name, str):
                    raise HostReleaseError("HostRelease image config missing")
                config_file = outer.extractfile(config_name)
                config_bytes = config_file.read(_MAX_JSON_BYTES + 1) if config_file else b""
                if (
                    not config_bytes
                    or len(config_bytes) > _MAX_JSON_BYTES
                    or not isinstance(json.loads(config_bytes), dict)
                    or f"sha256:{hashlib.sha256(config_bytes).hexdigest()}" != host["bundle"]["image_id"]
                ):
                    raise HostReleaseError("HostRelease image config identity mismatch")
                layers = image_record.get("Layers")
                if not isinstance(layers, list) or len(layers) != 1 or not isinstance(layers[0], str):
                    raise HostReleaseError("HostRelease bundle must contain one scratch layer")
                layer_member = outer.getmember(layers[0])
                if not layer_member.isfile() or layer_member.size > _MAX_LAYER_BYTES:
                    raise HostReleaseError("HostRelease layer invalid")
                layer_file = outer.extractfile(layer_member)
                if layer_file is None:
                    raise HostReleaseError("HostRelease layer missing")
                layer_bytes = layer_file.read(_MAX_LAYER_BYTES + 1)
        except HostReleaseError:
            raise
        except (OSError, KeyError, tarfile.TarError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HostReleaseError("HostRelease OCI image invalid") from exc
        if len(layer_bytes) > _MAX_LAYER_BYTES:
            raise HostReleaseError("HostRelease layer too large")
        expected = {record["path"]: record for record in host["files"]}
        observed: set[str] = set()
        try:
            with tarfile.open(fileobj=io.BytesIO(layer_bytes), mode="r:*") as layer:
                for member in layer:
                    name = member.name.removeprefix("./")
                    if name == "host-release" and member.isdir():
                        continue
                    if name.startswith("host-release/"):
                        relative = name.removeprefix("host-release/")
                    else:
                        raise HostReleaseError("HostRelease layer contains content outside /host-release")
                    if member.isdir():
                        clean = relative.rstrip("/")
                        path = PurePosixPath(clean)
                        if (
                            not clean
                            or path.is_absolute()
                            or any(part in {"", ".", ".."} for part in path.parts)
                            or not any(expected_path.startswith(f"{clean}/") for expected_path in expected)
                        ):
                            raise HostReleaseError("HostRelease layer directory invalid")
                        continue
                    if not member.isfile() or member.islnk() or member.issym():
                        raise HostReleaseError("HostRelease layer contains non-regular content")
                    relative = _safe_path(relative)
                    record = expected.get(relative)
                    if record is None or relative in observed:
                        raise HostReleaseError("HostRelease layer contains unlisted content")
                    if member.size != record["size"] or stat.S_IMODE(member.mode) != record["mode"]:
                        raise HostReleaseError("HostRelease layer file metadata mismatch")
                    source = layer.extractfile(member)
                    data = source.read(record["size"] + 1) if source is not None else b""
                    if len(data) != record["size"] or f"sha256:{hashlib.sha256(data).hexdigest()}" != record["sha256"]:
                        raise HostReleaseError("HostRelease layer file digest mismatch")
                    destination = rootfs / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_write(destination, data, mode=record["mode"])
                    observed.add(relative)
        except HostReleaseError:
            raise
        except (OSError, tarfile.TarError) as exc:
            raise HostReleaseError("HostRelease layer invalid") from exc
        if observed != set(expected):
            raise HostReleaseError("HostRelease layer file set incomplete")

    def _validate_compose(self, host: dict[str, Any], software: dict[str, Any], rootfs: Path) -> None:
        compose = rootfs / "docker-compose.yml"
        try:
            compose_bytes = compose.read_bytes()
        except OSError as exc:
            raise HostReleaseError("HostRelease Compose missing") from exc
        if f"sha256:{hashlib.sha256(compose_bytes).hexdigest()}" != host["topology"]["compose_digest"]:
            raise HostReleaseError("HostRelease Compose digest mismatch")
        override_lines = ["services:"]
        for service in sorted(software["services"]):
            override_lines.extend(
                [
                    f"  {service}:",
                    f"    image: {json.dumps(_local_image_reference(service, software['image_ids'][service]))}",
                ]
            )
        with tempfile.TemporaryDirectory(prefix="drycontroller-host-compose-") as temporary:
            override = Path(temporary) / "software-images.yml"
            _atomic_write(override, ("\n".join(override_lines) + "\n").encode())
            output = self._command_runner(
                [
                    _DOCKER_COMPOSE,
                    "--env-file",
                    str(self.paths.runtime_env),
                    "--file",
                    str(compose),
                    "--file",
                    str(override),
                    "config",
                    "--format",
                    "json",
                ],
                rootfs,
                None,
            )
        try:
            config = json.loads(output)
        except json.JSONDecodeError as exc:
            raise HostReleaseError("HostRelease Compose config invalid") from exc
        services = config.get("services") if isinstance(config, dict) else None
        expected_inventory = host["topology"]["service_inventory"]
        if not isinstance(services, dict) or sorted(services) != expected_inventory:
            raise HostReleaseError("HostRelease Compose service inventory mismatch")
        if config.get("configs") or config.get("secrets"):
            raise HostReleaseError("HostRelease Compose external file injection forbidden")
        project_volumes = config.get("volumes") or {}
        if not isinstance(project_volumes, dict):
            raise HostReleaseError("HostRelease Compose named volumes invalid")
        for name, definition in project_volumes.items():
            if (
                not isinstance(name, str)
                or not _SERVICE_NAME.fullmatch(name)
                or not isinstance(definition, dict)
                or set(definition) != {"name"}
                or definition.get("name") != f"drycontroller-edge_{name}"
            ):
                raise HostReleaseError("HostRelease Compose named volume forbidden")
        for service, definition in services.items():
            if not isinstance(definition, dict) or any(key in definition for key in _FORBIDDEN_COMPOSE_KEYS):
                raise HostReleaseError("HostRelease Compose contains forbidden privilege")
            image = definition.get("image")
            if image != _local_image_reference(service, software["image_ids"][service]):
                raise HostReleaseError("HostRelease Compose image is not the paired local image")
            for volume in definition.get("volumes") or []:
                if not isinstance(volume, dict):
                    raise HostReleaseError("HostRelease Compose volume invalid")
                volume_type = volume.get("type")
                source = volume.get("source")
                read_only = volume.get("read_only") is True
                if volume_type == "volume":
                    if source not in project_volumes:
                        raise HostReleaseError("HostRelease Compose named volume forbidden")
                    continue
                if volume_type != "bind" or not isinstance(source, str) or not source.startswith("/"):
                    raise HostReleaseError("HostRelease Compose volume type forbidden")
                if isinstance(source, str) and (source == "/var/run/docker.sock" or source.startswith("/run/docker")):
                    raise HostReleaseError("HostRelease Compose may not mount the Docker socket")
                if isinstance(source, str) and source.startswith("/"):
                    source_path = Path(source)
                    try:
                        relative = source_path.relative_to(rootfs.resolve()).as_posix()
                    except ValueError:
                        writable_exact = {
                            "/etc/drycontroller/license",
                            "/var/lib/drycontroller/configs-runtime",
                            "/var/lib/drycontroller/discovery",
                        }
                        readonly_exact = {
                            "/etc/drycontroller/keys",
                            "/var/lib/drycontroller",
                            "/run/drycontroller-ota",
                            "/run/drycontroller-host-release",
                            "/run/drycontroller-registry-auth",
                            "/var/log/drycontroller",
                        }
                        service_writable = source.removeprefix("/var/log/drycontroller/")
                        service_model = source.removeprefix("/var/lib/drycontroller/models/")
                        allowed = (
                            source in writable_exact
                            or (source in readonly_exact and read_only)
                            or (
                                source.startswith("/var/log/drycontroller/")
                                and service_writable in expected_inventory
                            )
                            or (
                                source.startswith("/var/lib/drycontroller/models/")
                                and service_model in expected_inventory
                                and read_only
                            )
                        )
                        if not allowed:
                            raise HostReleaseError("HostRelease Compose host bind path forbidden")
                    else:
                        signed_paths = {record["path"] for record in host["files"]}
                        if relative in {"", "."} or not any(
                            path == relative or path.startswith(f"{relative}/") for path in signed_paths
                        ):
                            raise HostReleaseError("HostRelease Compose host bind path is not signed")
                        if not read_only:
                            raise HostReleaseError("HostRelease signed files must be mounted read-only")

    def _stage_software_images(self, journal: dict[str, Any], policy: dict[str, Any]) -> None:
        software = policy["software_manifest"]
        release_dir = self.paths.software_releases / policy["software_manifest_digest"].removeprefix("sha256:")
        release_dir.mkdir(parents=True, exist_ok=True)
        expected: dict[str, str] = {}
        for service in sorted(software["services"]):
            remote = software["services"][service]
            image_id = software["image_ids"][service]
            local = _local_image_reference(service, image_id)
            self._effect(
                journal,
                f"software_pull_{service}",
                lambda remote=remote: self._command_runner([_DOCKER, "pull", remote], None, None),
            )
            self._effect(
                journal,
                f"software_cosign_{service}",
                lambda remote=remote: self._command_runner(
                    [
                        _COSIGN,
                        "verify",
                        "--insecure-ignore-tlog=true",
                        "--key",
                        str(self.paths.software_cosign_pubkey),
                        remote,
                    ],
                    None,
                    None,
                ),
            )
            self._effect(
                journal,
                f"software_tag_{service}",
                lambda remote=remote, local=local: self._command_runner(
                    [_DOCKER, "tag", remote, local],
                    None,
                    None,
                ),
            )
            expected[local] = image_id
        archive = release_dir / ".image-verification.tar"
        try:
            self._effect(
                journal,
                "software_image_archive",
                lambda: self._command_runner(
                    [_DOCKER, "image", "save", "-o", str(archive), *expected],
                    None,
                    None,
                ),
            )
            actual = _docker_archive_digests(archive)
        finally:
            archive.unlink(missing_ok=True)
        if any(actual.get(reference) != image_id for reference, image_id in expected.items()):
            raise HostReleaseError("SoftwareRelease image ID mismatch")
        evidence = {
            "manifest_digest": policy["software_manifest_digest"],
            "images": {},
        }
        for service in sorted(software["services"]):
            image_id = software["image_ids"][service]
            reference = _local_image_reference(service, image_id)
            runtime_id = self._command_runner(
                [_DOCKER, "image", "inspect", "--format", "{{.Id}}", reference],
                None,
                None,
            ).strip()
            if not _DIGEST.fullmatch(runtime_id):
                raise HostReleaseError("SoftwareRelease runtime image ID invalid")
            evidence["images"][service] = {
                "reference": reference,
                "image_id": image_id,
                "runtime_id": runtime_id,
            }
        _atomic_write(release_dir / "local-images.json", canonical_json_bytes(evidence))

    def _stage_initial_software_images(
        self,
        software: dict[str, Any],
        manifest_digest: str,
        image_archive: Path,
    ) -> None:
        expected = {
            _local_image_reference(service, software["image_ids"][service]): software["image_ids"][service]
            for service in sorted(software["services"])
        }
        actual = _docker_archive_digests(image_archive)
        if actual != expected:
            raise HostReleaseError("initial SoftwareRelease image archive mismatch")
        self._command_runner([_DOCKER, "image", "load", "-i", str(image_archive)], None, None)
        release_dir = self.paths.software_releases / manifest_digest.removeprefix("sha256:")
        release_dir.mkdir(parents=True, exist_ok=False)
        evidence: dict[str, Any] = {"manifest_digest": manifest_digest, "images": {}}
        for service in sorted(software["services"]):
            image_id = software["image_ids"][service]
            reference = _local_image_reference(service, image_id)
            runtime_id = self._command_runner(
                [_DOCKER, "image", "inspect", "--format", "{{.Id}}", reference],
                None,
                None,
            ).strip()
            if not _DIGEST.fullmatch(runtime_id):
                raise HostReleaseError("initial SoftwareRelease runtime image ID invalid")
            evidence["images"][service] = {
                "reference": reference,
                "image_id": image_id,
                "runtime_id": runtime_id,
            }
        _atomic_write(release_dir / "local-images.json", canonical_json_bytes(evidence))

    def _slot(self, policy: dict[str, Any], rootfs: Path) -> dict[str, Any]:
        return {
            "host_manifest": policy["host_manifest"],
            "host_manifest_digest": policy["host_manifest_digest"],
            "host_manifest_signature": policy["host_manifest_signature"],
            "software_manifest": policy["software_manifest"],
            "software_manifest_digest": policy["software_manifest_digest"],
            "software_manifest_signature": policy["software_manifest_signature"],
            "host_path": str(rootfs),
        }

    @staticmethod
    def _initial_seed(
        seed: object,
        *,
        validator: Callable[[object], dict[str, Any]],
        label: str,
    ) -> tuple[dict[str, Any], str, str]:
        if not isinstance(seed, dict) or set(seed) != {
            "manifest",
            "manifest_digest",
            "manifest_signature",
        }:
            raise HostReleaseError(f"initial {label} seed fields invalid")
        manifest = validator(seed["manifest"])
        manifest_digest = canonical_digest(manifest)
        if seed["manifest_digest"] != manifest_digest:
            raise HostReleaseError(f"initial {label} seed digest mismatch")
        signature = seed["manifest_signature"]
        if not isinstance(signature, str):
            raise HostReleaseError(f"initial {label} seed signature invalid")
        try:
            signature_bytes = base64.b64decode(signature, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HostReleaseError(f"initial {label} seed signature invalid") from exc
        if not signature_bytes or len(signature_bytes) > 16 * 1024:
            raise HostReleaseError(f"initial {label} seed signature invalid")
        return manifest, manifest_digest, signature

    def _initial_slot(
        self,
        *,
        host: dict[str, Any],
        host_digest: str,
        host_signature: str,
        software: dict[str, Any],
        software_digest: str,
        software_signature: str,
    ) -> dict[str, Any]:
        return self._validate_slot(
            {
                "host_manifest": host,
                "host_manifest_digest": host_digest,
                "host_manifest_signature": host_signature,
                "software_manifest": software,
                "software_manifest_digest": software_digest,
                "software_manifest_signature": software_signature,
                "host_path": str(self._release_dir(host)),
            }
        )

    def _initial_journal(self, slot: dict[str, Any], phase: str) -> dict[str, Any]:
        return {
            "schema": COORDINATOR_JOURNAL_SCHEMA,
            "phase": phase,
            "operation_id": None,
            "last_operation_id": None,
            "high_watermark": 0,
            "active": None,
            "candidate": slot,
            "previous": None,
            "effect": None,
            "error": None,
        }

    def _remove_initial_state(self, slot: dict[str, Any]) -> None:
        rootfs = Path(slot["host_path"])
        if self.paths.current_link.is_symlink():
            if self.paths.current_link.readlink() != rootfs:
                raise HostReleaseError("initial HostRelease current link conflict")
            self._stop_active(slot)
            self.paths.current_link.unlink()
            _fsync_directory(self.paths.current_link.parent)
        for state_file in (self.paths.host_active, self.paths.software_journal):
            state_file.unlink(missing_ok=True)
            _fsync_directory(state_file.parent)
        for directory in (
            rootfs,
            self._staging_dir(slot["host_manifest"]),
            self.paths.software_releases
            / slot["software_manifest_digest"].removeprefix("sha256:"),
        ):
            if directory.exists():
                if directory.is_symlink() or not directory.is_dir():
                    raise HostReleaseError("initial release path conflict")
                shutil.rmtree(directory)
                _fsync_directory(directory.parent)

    def _recover_initialization(self, slot: dict[str, Any]) -> None:
        journal = self._load_journal()
        if (
            not journal["phase"].startswith("initial_")
            or journal["active"] is not None
            or journal["previous"] is not None
            or journal["candidate"] != slot
        ):
            raise HostReleaseError("existing HostRelease state is not an interrupted initial install")
        journal["phase"] = "initial_cleanup"
        self._persist(journal)
        self._remove_initial_state(slot)
        self.paths.journal.unlink()
        _fsync_directory(self.paths.journal.parent)

    def _verify_initial_active_state(self, slot: dict[str, Any]) -> None:
        if (
            not self.paths.current_link.is_symlink()
            or self.paths.current_link.readlink() != Path(slot["host_path"])
            or not self.paths.compatibility_link.is_symlink()
            or self.paths.compatibility_link.readlink() != self.paths.current_link
            or _read_json(self.paths.host_active) != self._host_active(slot)
        ):
            raise HostReleaseError("initial active HostRelease state invalid")
        software_journal = _read_json(self.paths.software_journal)
        software_active = software_journal.get("active") if isinstance(software_journal, dict) else None
        if (
            not isinstance(software_journal, dict)
            or software_journal.get("schema") != "drycontroller.release-journal.v1"
            or software_journal.get("phase") != "idle"
            or software_journal.get("operation_id") is not None
            or software_journal.get("last_operation_id") is not None
            or software_journal.get("candidate") is not None
            or software_journal.get("previous") is not None
            or software_journal.get("effect") is not None
            or software_journal.get("error") is not None
            or not isinstance(software_active, dict)
            or software_active.get("manifest") != slot["software_manifest"]
            or software_active.get("manifest_digest") != slot["software_manifest_digest"]
            or software_active.get("manifest_signature") != slot["software_manifest_signature"]
            or not isinstance(software_active.get("verified_at"), str)
        ):
            raise HostReleaseError("initial active SoftwareRelease state invalid")
        self._verify_slot_offline(slot)

    def initialize_initial_pair(
        self,
        *,
        host_seed: object,
        software_seed: object,
        host_image_archive: Path,
        software_image_archive: Path,
    ) -> dict[str, Any]:
        host, host_digest, host_signature = self._initial_seed(
            host_seed,
            validator=validate_host_manifest,
            label="HostRelease",
        )
        software, software_digest, software_signature = self._initial_seed(
            software_seed,
            validator=validate_software_manifest,
            label="SoftwareRelease",
        )
        assert_pair(host, software)
        slot = self._initial_slot(
            host=host,
            host_digest=host_digest,
            host_signature=host_signature,
            software=software,
            software_digest=software_digest,
            software_signature=software_signature,
        )
        with self._transaction_lock() as lock_descriptor:
            if self.paths.journal.exists():
                journal = self._load_journal()
                expected_complete = {
                    **self._initial_journal(slot, "idle"),
                    "active": slot,
                    "candidate": None,
                }
                if journal == expected_complete:
                    self._verify_initial_active_state(slot)
                    return {
                        "host_release_id": host["release_id"],
                        "software_release_id": software["release_id"],
                        "status": "already_initialized",
                    }
            self._assert_fresh_install(lock_descriptor)
            if self.paths.journal.exists():
                self._recover_initialization(slot)

            if any(
                path.exists() or path.is_symlink()
                for path in (
                    self.paths.current_link,
                    self.paths.host_active,
                    self.paths.software_journal,
                    Path(slot["host_path"]),
                    self.paths.software_releases / software_digest.removeprefix("sha256:"),
                )
            ):
                raise HostReleaseError("initial release requires blank host state")
            if (
                not self.paths.compatibility_link.is_symlink()
                or self.paths.compatibility_link.readlink() != self.paths.current_link
            ):
                raise HostReleaseError("HostRelease compatibility link is not the immutable indirection")

            self._verify_key(self.paths.host_cosign_pubkey, host["signing_key_sha256"])
            self._verify_key(self.paths.software_cosign_pubkey, software["signing_key_sha256"])
            self._verify_blob_signature(
                manifest=host,
                signature=host_signature,
                public_key=self.paths.host_cosign_pubkey,
                prefix="host",
            )
            self._verify_blob_signature(
                manifest=software,
                signature=software_signature,
                public_key=self.paths.software_cosign_pubkey,
                prefix="software",
            )

            journal = self._initial_journal(slot, "initial_staging")
            self._persist(journal)
            staging = self._staging_dir(host)
            try:
                staging.mkdir(parents=True, mode=0o700)
                self._effect(
                    journal,
                    "initial_host_extract",
                    lambda: self._extract_bundle(host_image_archive, host, staging),
                )
                rootfs = staging / "rootfs"
                self._effect(
                    journal,
                    "initial_compose_verify",
                    lambda: self._validate_compose(host, software, rootfs),
                )
                _atomic_write(rootfs / ".host-release-manifest.json", canonical_json_bytes(host))
                _atomic_write(rootfs / ".host-release-signature.txt", host_signature.encode())
                os.replace(rootfs, Path(slot["host_path"]))
                _fsync_directory(Path(slot["host_path"]).parent)
                shutil.rmtree(staging)
                _fsync_directory(staging.parent)
                self._effect(
                    journal,
                    "initial_software_images",
                    lambda: self._stage_initial_software_images(
                        software,
                        software_digest,
                        software_image_archive,
                    ),
                )
                journal["phase"] = "initial_activating"
                journal["effect"] = None
                self._persist(journal)
                self._atomic_links(Path(slot["host_path"]))
                _atomic_write(self.paths.host_active, canonical_json_bytes(self._host_active(slot)))
                self._reset_software_journal(slot)
                self._verify_slot_offline(slot)
                journal["phase"] = "initial_starting"
                self._persist(journal)
                self._start_slot(slot, lock_descriptor)
            except (HostReleaseError, OSError) as exc:
                journal["phase"] = "initial_cleanup"
                journal["error"] = str(exc)[:512]
                self._persist(journal)
                try:
                    self._remove_initial_state(slot)
                    self.paths.journal.unlink()
                    _fsync_directory(self.paths.journal.parent)
                except (HostReleaseError, OSError) as cleanup_exc:
                    journal["phase"] = "initial_cleanup_failed"
                    journal["error"] = str(cleanup_exc)[:512]
                    self._persist(journal)
                raise HostReleaseError("initial H1/T1/S1 activation failed") from exc

            self._verify_initial_active_state(slot)
            journal = {
                **self._initial_journal(slot, "idle"),
                "active": slot,
                "candidate": None,
            }
            self._persist(journal)
            return {
                "host_release_id": host["release_id"],
                "software_release_id": software["release_id"],
                "status": "initialized",
            }

    def _assert_fresh_install(self, lock_descriptor: int) -> None:
        self._command_runner(
            [
                _BASH,
                _FRESH_INSTALL_GUARD,
                "--project",
                _FIXED_ENV["COMPOSE_PROJECT_NAME"],
                "--lock-fd",
                str(lock_descriptor),
            ],
            None,
            None,
        )

    def _validate_slot(self, slot: object) -> dict[str, Any]:
        fields = {
            "host_manifest",
            "host_manifest_digest",
            "host_manifest_signature",
            "software_manifest",
            "software_manifest_digest",
            "software_manifest_signature",
            "host_path",
        }
        if not isinstance(slot, dict) or set(slot) != fields:
            raise HostReleaseError("HostRelease coordinator slot invalid")
        host = validate_host_manifest(slot["host_manifest"])
        software = validate_software_manifest(slot["software_manifest"])
        assert_pair(host, software)
        if slot["host_manifest_digest"] != canonical_digest(host) or slot["software_manifest_digest"] != canonical_digest(software):
            raise HostReleaseError("HostRelease coordinator slot digest invalid")
        expected_path = self._release_dir(host)
        if Path(slot["host_path"]) != expected_path:
            raise HostReleaseError("HostRelease coordinator slot path invalid")
        return slot

    def _active_slot_from_disk(self) -> dict[str, Any]:
        journal = _read_json(self.paths.software_journal)
        host_active = _read_json(self.paths.host_active)
        if not isinstance(journal, dict) or not isinstance(host_active, dict):
            raise HostReleaseError("active H/S state invalid")
        software_slot = journal.get("active")
        if not isinstance(software_slot, dict):
            raise HostReleaseError("active SoftwareRelease missing")
        software = validate_software_manifest(software_slot.get("manifest"))
        host_id = host_active.get("host_release_id")
        host_dir = self.paths.releases / str(host_id)
        host_manifest = _read_json(host_dir / ".host-release-manifest.json")
        host_signature = (host_dir / ".host-release-signature.txt").read_text(encoding="utf-8")
        host = validate_host_manifest(host_manifest)
        assert_pair(host, software)
        return self._validate_slot(
            {
                "host_manifest": host,
                "host_manifest_digest": canonical_digest(host),
                "host_manifest_signature": host_signature,
                "software_manifest": software,
                "software_manifest_digest": software_slot.get("manifest_digest"),
                "software_manifest_signature": software_slot.get("manifest_signature"),
                "host_path": str(host_dir),
            }
        )

    def _verify_slot_offline(self, slot: dict[str, Any]) -> None:
        slot = self._validate_slot(slot)
        rootfs = Path(slot["host_path"])
        if not rootfs.is_dir():
            raise HostReleaseError("HostRelease directory unavailable")
        self._verify_key(self.paths.host_cosign_pubkey, slot["host_manifest"]["signing_key_sha256"])
        self._verify_key(self.paths.software_cosign_pubkey, slot["software_manifest"]["signing_key_sha256"])
        self._verify_blob_signature(
            manifest=slot["host_manifest"],
            signature=slot["host_manifest_signature"],
            public_key=self.paths.host_cosign_pubkey,
            prefix="host",
        )
        self._verify_blob_signature(
            manifest=slot["software_manifest"],
            signature=slot["software_manifest_signature"],
            public_key=self.paths.software_cosign_pubkey,
            prefix="software",
        )
        self._validate_compose(slot["host_manifest"], slot["software_manifest"], rootfs)
        expected_paths = {record["path"] for record in slot["host_manifest"]["files"]}
        metadata_paths = {".host-release-manifest.json", ".host-release-signature.txt"}
        observed_paths: set[str] = set()
        for path in rootfs.rglob("*"):
            relative = path.relative_to(rootfs).as_posix()
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                if path.is_symlink():
                    raise HostReleaseError("HostRelease previous directory changed")
                continue
            if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
                raise HostReleaseError("HostRelease previous contains non-regular content")
            observed_paths.add(relative)
        if observed_paths != expected_paths | metadata_paths:
            raise HostReleaseError("HostRelease previous file set changed")
        if (
            _read_json(rootfs / ".host-release-manifest.json") != slot["host_manifest"]
            or (rootfs / ".host-release-signature.txt").read_text(encoding="utf-8")
            != slot["host_manifest_signature"]
        ):
            raise HostReleaseError("HostRelease previous metadata changed")
        for record in slot["host_manifest"]["files"]:
            path = rootfs / record["path"]
            try:
                metadata = path.lstat()
                data = path.read_bytes()
            except OSError as exc:
                raise HostReleaseError("HostRelease previous file unavailable") from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != record["mode"]
                or len(data) != record["size"]
                or f"sha256:{hashlib.sha256(data).hexdigest()}" != record["sha256"]
            ):
                raise HostReleaseError("HostRelease previous file changed")
        evidence = _read_json(
            self.paths.software_releases
            / slot["software_manifest_digest"].removeprefix("sha256:")
            / "local-images.json"
        )
        if not isinstance(evidence, dict) or evidence.get("manifest_digest") != slot["software_manifest_digest"]:
            raise HostReleaseError("SoftwareRelease local image evidence invalid")
        for service, image_id in slot["software_manifest"]["image_ids"].items():
            record = evidence.get("images", {}).get(service)
            expected_reference = _local_image_reference(service, image_id)
            if (
                not isinstance(record, dict)
                or record.get("reference") != expected_reference
                or record.get("image_id") != image_id
            ):
                raise HostReleaseError("SoftwareRelease local image evidence invalid")
            actual = self._command_runner(
                [_DOCKER, "image", "inspect", "--format", "{{.Id}}", expected_reference],
                None,
                None,
            ).strip()
            if actual != record.get("runtime_id"):
                raise HostReleaseError("SoftwareRelease previous image unavailable")

    def _discard_candidate_files(self, host: dict[str, Any], software: dict[str, Any]) -> None:
        release_dir = self._release_dir(host)
        for link in (self.paths.current_link, self.paths.compatibility_link):
            if link.is_symlink() and link.resolve() == release_dir:
                raise HostReleaseError("refusing to discard an active HostRelease")
        for directory in (self._staging_dir(host), release_dir):
            if directory.exists():
                if directory.is_symlink() or not directory.is_dir():
                    raise HostReleaseError("HostRelease candidate path invalid")
                shutil.rmtree(directory)
                _fsync_directory(directory.parent)
        software_dir = self.paths.software_releases / canonical_digest(software).removeprefix("sha256:")
        if software_dir.exists():
            if software_dir.is_symlink() or not software_dir.is_dir():
                raise HostReleaseError("SoftwareRelease candidate path invalid")
            shutil.rmtree(software_dir)
            _fsync_directory(software_dir.parent)

    def _stage_candidate(self, journal: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
        host = policy["host_manifest"]
        software = policy["software_manifest"]
        staging = self._staging_dir(host)
        final = self._release_dir(host)
        if final.exists():
            raise HostReleaseError("HostRelease directory already exists")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, mode=0o700)
        self._verify_key(self.paths.host_cosign_pubkey, host["signing_key_sha256"])
        self._verify_key(self.paths.software_cosign_pubkey, software["signing_key_sha256"])
        self._verify_blob_signature(
            manifest=host,
            signature=policy["host_manifest_signature"],
            public_key=self.paths.host_cosign_pubkey,
            prefix="host",
        )
        self._verify_blob_signature(
            manifest=software,
            signature=policy["software_manifest_signature"],
            public_key=self.paths.software_cosign_pubkey,
            prefix="software",
        )
        archive = self._effect(journal, "bundle_pull_verify_export", lambda: self._pull_and_export_bundle(host, staging))
        self._effect(journal, "bundle_extract_verify", lambda: self._extract_bundle(archive, host, staging))
        rootfs = staging / "rootfs"
        self._effect(journal, "candidate_compose_verify", lambda: self._validate_compose(host, software, rootfs))
        _atomic_write(rootfs / ".host-release-manifest.json", canonical_json_bytes(host))
        _atomic_write(rootfs / ".host-release-signature.txt", policy["host_manifest_signature"].encode())
        os.replace(rootfs, final)
        _fsync_directory(final.parent)
        archive.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
        _fsync_directory(staging.parent)
        return self._slot(policy, final)

    def _atomic_links(self, target: Path) -> None:
        if not target.is_dir():
            raise HostReleaseError("HostRelease link target unavailable")
        try:
            compatibility_target = self.paths.compatibility_link.readlink()
        except OSError as exc:
            raise HostReleaseError("HostRelease compatibility link unavailable") from exc
        if compatibility_target != self.paths.current_link:
            raise HostReleaseError("HostRelease compatibility link is not the immutable indirection")
        link = self.paths.current_link
        link.parent.mkdir(parents=True, exist_ok=True)
        temporary = link.with_name(f".{link.name}.{os.getpid()}.tmp")
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(target)
        os.replace(temporary, link)
        directory_fd = os.open(link.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _software_seed(self, slot: dict[str, Any]) -> dict[str, Any]:
        return {
            "manifest": slot["software_manifest"],
            "manifest_digest": slot["software_manifest_digest"],
            "manifest_signature": slot["software_manifest_signature"],
        }

    def _host_active(self, slot: dict[str, Any]) -> dict[str, Any]:
        host = slot["host_manifest"]
        return {
            "schema": HOST_ACTIVE_SCHEMA,
            "host_release_id": host["release_id"],
            "host_release_manifest_digest": slot["host_manifest_digest"],
            "topology_digest": host["topology_digest"],
            "compose_digest": host["topology"]["compose_digest"],
            "service_inventory": host["topology"]["service_inventory"],
        }

    def _stop_active(self, slot: dict[str, Any]) -> None:
        self._command_runner(
            [
                _DOCKER_COMPOSE,
                "--env-file",
                str(self.paths.runtime_env),
                "--file",
                str(Path(slot["host_path"]) / "docker-compose.yml"),
                "down",
            ],
            Path(slot["host_path"]),
            None,
        )

    def _reset_software_journal(self, slot: dict[str, Any]) -> None:
        seed = self._software_seed(slot)
        active = {
            **seed,
            "verified_at": utc_now(),
        }
        journal = {
            "schema": "drycontroller.release-journal.v1",
            "phase": "idle",
            "operation_id": None,
            "last_operation_id": None,
            "active": active,
            "candidate": None,
            "previous": None,
            "effect": None,
            "error": None,
        }
        _atomic_write(self.paths.software_journal, canonical_json_bytes(journal))

    def _start_slot(self, slot: dict[str, Any], lock_descriptor: int) -> None:
        self._command_runner(
            [
                str(Path(slot["host_path"]) / "infra/ota-helper/start-active-release"),
                "--lock-fd",
                str(lock_descriptor),
            ],
            Path(slot["host_path"]),
            None,
        )

    def _restart_software_helper(self) -> None:
        self._command_runner(
            [_SYSTEMCTL, "restart", "drycontroller-ota-helper.service"],
            None,
            None,
        )

    def _activate_slot(
        self,
        journal: dict[str, Any],
        slot: dict[str, Any],
        prefix: str,
        lock_descriptor: int,
    ) -> None:
        self._effect(journal, f"{prefix}_links", lambda: self._atomic_links(Path(slot["host_path"])))
        self._effect(journal, f"{prefix}_host_active", lambda: _atomic_write(self.paths.host_active, canonical_json_bytes(self._host_active(slot))))
        self._effect(journal, f"{prefix}_software_journal", lambda: self._reset_software_journal(slot))
        self._effect(journal, f"{prefix}_software_helper_restart", self._restart_software_helper)
        self._effect(
            journal,
            f"{prefix}_start_health",
            lambda: self._start_slot(slot, lock_descriptor),
        )

    def _rollback(self, journal: dict[str, Any], reason: str, lock_descriptor: int) -> None:
        previous = self._validate_slot(journal.get("previous"))
        candidate = self._validate_slot(journal["candidate"]) if journal.get("candidate") is not None else None
        journal["phase"] = "rollback_started"
        journal["error"] = reason[:512]
        self._persist(journal)
        self._verify_slot_offline(previous)
        self._activate_slot(journal, previous, "previous", lock_descriptor)
        if candidate is not None:
            self._discard_candidate_files(candidate["host_manifest"], candidate["software_manifest"])
        journal.update(
            {
                "phase": "idle",
                "operation_id": None,
                "active": previous,
                "candidate": None,
                "previous": None,
                "effect": None,
                "error": reason[:512],
            }
        )
        self._persist(journal)

    def _recover(self, journal: dict[str, Any], lock_descriptor: int) -> None:
        if journal.get("previous") is None:
            raise HostReleaseError("interrupted HostRelease has no previous slot")
        self._rollback(journal, "interrupted_host_release_recovered", lock_descriptor)
        raise HostReleaseError("interrupted_host_release_recovered")

    def apply(self) -> dict[str, Any]:
        with self._transaction_lock() as lock_descriptor:
            operation_id = self._read_operation_id()
            journal = self._load_journal()
            if journal["phase"] != "idle":
                self._recover(journal, lock_descriptor)
            sequence = int(operation_id[:16], 16)
            if sequence < journal["high_watermark"]:
                raise HostReleaseError("HostRelease operation sequence is stale")
            if sequence == journal["high_watermark"]:
                if operation_id == journal["last_operation_id"]:
                    active = self._validate_slot(journal["active"])
                    return {
                        "operation_id": operation_id,
                        "host_release_id": active["host_manifest"]["release_id"],
                        "software_release_id": active["software_manifest"]["release_id"],
                        "status": "succeeded",
                    }
                raise HostReleaseError("HostRelease operation sequence conflict")
            active = self._validate_slot(journal.get("active")) if journal.get("active") is not None else self._active_slot_from_disk()
            policy = self._validate_policy(self._policy_fetcher(operation_id), operation_id)
            candidate_host = policy["host_manifest"]
            if candidate_host["sequence"] <= active["host_manifest"]["sequence"]:
                raise HostReleaseError("HostRelease manifest sequence is not newer")
            journal.update(
                {
                    "phase": "candidate_staging",
                    "operation_id": operation_id,
                    "active": active,
                    "candidate": self._slot(policy, self._release_dir(candidate_host)),
                    "previous": active,
                    "effect": None,
                    "error": None,
                }
            )
            self._persist(journal)
            mutation_started = False
            try:
                candidate = self._stage_candidate(journal, policy)
                self._stage_software_images(journal, policy)
                journal["candidate"] = candidate
                journal["phase"] = "candidate_verified"
                self._persist(journal)
                self._verify_slot_offline(active)
                journal["phase"] = "previous_proven"
                self._persist(journal)
                mutation_started = True
                journal["phase"] = "active_stopping"
                self._persist(journal)
                self._effect(journal, "active_stop", lambda: self._stop_active(active))
                journal["phase"] = "candidate_activating"
                self._persist(journal)
                self._activate_slot(journal, candidate, "candidate", lock_descriptor)
                journal["phase"] = "promotion_started"
                self._persist(journal)
            except HostReleaseError as exc:
                if mutation_started:
                    self._rollback(journal, str(exc), lock_descriptor)
                else:
                    self._discard_candidate_files(policy["host_manifest"], policy["software_manifest"])
                    journal.update(
                        {
                            "phase": "idle",
                            "operation_id": None,
                            "candidate": None,
                            "previous": None,
                            "effect": None,
                            "error": str(exc)[:512],
                        }
                    )
                    self._persist(journal)
                raise
            journal.update(
                {
                    "phase": "idle",
                    "operation_id": None,
                    "last_operation_id": operation_id,
                    "high_watermark": sequence,
                    "active": candidate,
                    "candidate": None,
                    "previous": active,
                    "effect": None,
                    "error": None,
                }
            )
            self._persist(journal)
            return {
                "operation_id": operation_id,
                "host_release_id": candidate["host_manifest"]["release_id"],
                "software_release_id": candidate["software_manifest"]["release_id"],
                "status": "succeeded",
            }
