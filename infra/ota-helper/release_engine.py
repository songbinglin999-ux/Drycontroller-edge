#!/usr/bin/python3
"""Signed, digest-locked Edge release transaction with deterministic rollback."""

from __future__ import annotations

import base64
import binascii
import contextlib
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator


_SECURITY_CONTRACT_DIR = Path(__file__).resolve().parent
if str(_SECURITY_CONTRACT_DIR) not in sys.path:
    sys.path.insert(0, str(_SECURITY_CONTRACT_DIR))
from release_security_contract import (  # noqa: E402
    SHARED_SECURITY_CONTRACT_VERSION,
    assert_engine_security_contract,
)
if not SHARED_SECURITY_CONTRACT_VERSION.startswith("sha256:"):
    raise RuntimeError("release security contract version invalid")


SOFTWARE_RELEASE_SCHEMA = "drycontroller.software-release.v1"
RELEASE_JOURNAL_SCHEMA = "drycontroller.release-journal.v1"
REGISTRY_AUTH_SCHEMA = "drycontroller.registry-auth.v2"
HOST_ACTIVE_SCHEMA = "drycontroller.host-active.v1"
HOST_HEALTH_CONTRACT = "drycontroller.edge-health.v1"
COSIGN_VERSION = "v3.1.3"
COSIGN_LINUX_AMD64_SHA256 = "sha256:4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71"
COSIGN_TOOLCHAIN = {
    "version": COSIGN_VERSION,
    "linux_amd64_sha256": COSIGN_LINUX_AMD64_SHA256,
}
_MANIFEST_FIELDS = {
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
_HOST_ACTIVE_FIELDS = {
    "schema",
    "host_release_id",
    "host_release_manifest_digest",
    "topology_digest",
    "compose_digest",
    "service_inventory",
}
_POLICY_FIELDS = {
    "operation_id",
    "release_id",
    "manifest_digest",
    "manifest_signature",
    "manifest",
    "tenant_id",
    "line_id",
    "device_id",
    "node_name",
    "node_id",
}
_JOURNAL_FIELDS = {
    "schema",
    "phase",
    "operation_id",
    "last_operation_id",
    "active",
    "candidate",
    "previous",
    "effect",
    "error",
}
_OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REGISTRY = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?$")
_REPOSITORY_PATH = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")
_SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_HEALTH_URLS = (
    "http://127.0.0.1:18080/ready",
    "http://127.0.0.1:8082/ready",
)
_DOCKER = "/usr/bin/docker"
_DOCKER_COMPOSE = "/usr/libexec/docker/cli-plugins/docker-compose"
_COSIGN = "/usr/local/bin/cosign"
_MAX_JSON_BYTES = 1024 * 1024
_RUNTIME_READY_TIMEOUT_SECONDS = 180.0
_RUNTIME_READY_POLL_SECONDS = 2.0
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


class ReleaseError(RuntimeError):
    pass


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
        raise ReleaseError("runtime identity unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(payload, dict) or payload.get("schema") != schema:
        raise ReleaseError("runtime identity invalid")
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
        raise ReleaseError(f"fixed {label} executable unavailable") from exc
    try:
        identity = _read_identity_file(_TOOL_IDENTITY_PATH, "drycontroller.runtime-tools.v1")
    except ReleaseError:
        os.close(descriptor)
        raise
    executables = identity.get("executables")
    record = executables.get(value) if isinstance(executables, dict) else None
    if (
        not isinstance(executables, dict)
        or set(executables) != {_DOCKER, _DOCKER_COMPOSE, "/usr/bin/bash"}
        or not isinstance(record, dict)
        or set(record) != {"sha256"}
        or record["sha256"] != f"sha256:{digest.hexdigest()}"
    ):
        os.close(descriptor)
        raise ReleaseError(f"fixed {label} executable is untrusted")
    return descriptor


def _validate_trusted_executable(value: str, label: str) -> None:
    os.close(_open_trusted_executable(value, label))


def _descriptor_path(descriptor: int) -> str:
    prefix = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
    return f"{prefix}/{descriptor}"


@contextlib.contextmanager
def _opened_command(args: list[str]) -> Iterator[tuple[list[str], tuple[int, ...]]]:
    if not args or args[0] not in {_DOCKER, _DOCKER_COMPOSE}:
        yield args, ()
        return
    label = "Docker" if args[0] == _DOCKER else "Docker Compose"
    descriptor = _open_trusted_executable(args[0], label)
    try:
        yield [_descriptor_path(descriptor), *args[1:]], (descriptor,)
    finally:
        os.close(descriptor)


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
        raise ReleaseError("root registry auth source invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "auths", "credential"}
        or payload.get("schema") != REGISTRY_AUTH_SCHEMA
        or not isinstance(payload["auths"], dict)
    ):
        raise ReleaseError("root registry auth source schema invalid")
    normalized: dict[str, dict[str, str]] = {}
    for registry, entry in payload["auths"].items():
        if not isinstance(registry, str) or _REGISTRY.fullmatch(registry) is None:
            raise ReleaseError("root registry auth source schema invalid")
        if not isinstance(entry, dict) or set(entry) != {"auth"} or not isinstance(entry["auth"], str):
            raise ReleaseError("root registry auth source schema invalid")
        try:
            decoded = base64.b64decode(entry["auth"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ReleaseError("root registry auth source schema invalid") from exc
        username, separator, secret = decoded.partition(b":")
        if not username or separator != b":" or not secret or len(decoded) > 4096:
            raise ReleaseError("root registry auth source schema invalid")
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
        raise ReleaseError("root registry auth source schema invalid")
    try:
        bound = base64.b64decode(
            normalized[credential["registry_host"]]["auth"], validate=True
        ).partition(b":")
        bound_username = bound[0].decode("utf-8")
        bound_fingerprint = hashlib.sha256(bound[2]).hexdigest()[:16]
    except (UnicodeDecodeError, binascii.Error, ValueError) as exc:
        raise ReleaseError("root registry auth source schema invalid") from exc
    if bound_username != credential["username"] or not hmac.compare_digest(
        bound_fingerprint, credential["secret_fingerprint"]
    ):
        raise ReleaseError("root registry auth source schema invalid")
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
        raise ReleaseError("local Docker config invalid") from exc
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
        raise ReleaseError("Docker socket unavailable") from exc
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o002
        or metadata.st_nlink != 1
        or not stat.S_ISSOCK(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
    ):
        os.close(descriptor)
        raise ReleaseError("Docker socket is untrusted")
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
        raise ReleaseError("Docker daemon identity invalid")
    _validate_local_docker_config()
    descriptor = _open_trusted_executable(_DOCKER, "Docker")
    try:
        completed = subprocess.run(
            [
                _descriptor_path(descriptor),
                "info",
                "--format",
                "{{println .ID}}{{println .Name}}{{.DockerRootDir}}",
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
        raise ReleaseError("Docker daemon identity unavailable") from exc
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
        raise ReleaseError("Docker daemon identity mismatch")
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
                raise ReleaseError("Docker daemon identity changed")


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
                os.write(descriptor, canonical_manifest_bytes(payload))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            yield {**_FIXED_ENV, "DOCKER_CONFIG": str(directory)}
    except OSError as exc:
        raise ReleaseError("private Docker auth config unavailable") from exc


@dataclass(frozen=True)
class ReleasePaths:
    lock: Path
    journal: Path
    request: Path
    releases: Path
    project: Path
    compose: Path
    cosign_pubkey: Path
    registry_status: Path
    device: Path
    cloud_config: Path
    host_active: Path

    @classmethod
    def production(cls) -> "ReleasePaths":
        return cls(
            lock=Path("/var/lib/drycontroller-ota/release.lock"),
            journal=Path("/var/lib/drycontroller-ota/release-journal.json"),
            request=Path("/var/lib/drycontroller-ota/request.json"),
            releases=Path("/var/lib/drycontroller-ota/releases"),
            project=Path("/opt/Drycontroller-edge"),
            compose=Path("/opt/Drycontroller-edge/docker-compose.yml"),
            cosign_pubkey=Path("/etc/drycontroller/keys/cosign.pub"),
            registry_status=_REGISTRY_AUTH_SOURCE,
            device=Path("/var/lib/drycontroller/device.json"),
            cloud_config=Path("/etc/drycontroller/cloud-ops.env"),
            host_active=Path("/var/lib/drycontroller-host/active.json"),
        )


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_manifest_digest(manifest: dict[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()}"


def docker_archive_config_digests(path: Path) -> dict[str, str]:
    try:
        with tarfile.open(path, mode="r:*") as archive:
            manifest_file = archive.extractfile("manifest.json")
            if manifest_file is None:
                raise ReleaseError("Docker image archive manifest missing")
            manifest_bytes = manifest_file.read(_MAX_JSON_BYTES + 1)
            if len(manifest_bytes) > _MAX_JSON_BYTES:
                raise ReleaseError("Docker image archive manifest too large")
            manifest = json.loads(manifest_bytes)
            if (
                not isinstance(manifest, list)
                or not manifest
                or len(manifest) > 128
                or not all(isinstance(item, dict) for item in manifest)
            ):
                raise ReleaseError("Docker image archive manifest invalid")
            digests: dict[str, str] = {}
            for item in manifest:
                config_name = item.get("Config")
                repo_tags = item.get("RepoTags")
                if (
                    not isinstance(config_name, str)
                    or not config_name
                    or not isinstance(repo_tags, list)
                    or not repo_tags
                    or not all(isinstance(tag, str) and tag for tag in repo_tags)
                ):
                    raise ReleaseError("Docker image archive entry invalid")
                config_file = archive.extractfile(config_name)
                if config_file is None:
                    raise ReleaseError("Docker image archive config missing")
                config_bytes = config_file.read(_MAX_JSON_BYTES + 1)
                if len(config_bytes) > _MAX_JSON_BYTES:
                    raise ReleaseError("Docker image archive config too large")
                if not isinstance(json.loads(config_bytes), dict):
                    raise ReleaseError("Docker image archive config invalid")
                digest = f"sha256:{hashlib.sha256(config_bytes).hexdigest()}"
                for tag in repo_tags:
                    if tag in digests and digests[tag] != digest:
                        raise ReleaseError("Docker image archive tag is ambiguous")
                    digests[tag] = digest
    except ReleaseError:
        raise
    except (OSError, tarfile.TarError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("Docker image archive invalid") from exc
    return digests


def docker_archive_config_digest(path: Path) -> str:
    digests = set(docker_archive_config_digests(path).values())
    if len(digests) != 1:
        raise ReleaseError("Docker image archive must contain exactly one image")
    return digests.pop()


def local_image_reference(service: str, image_id: str) -> str:
    if _SERVICE_NAME.fullmatch(service) is None or _DIGEST.fullmatch(image_id) is None:
        raise ReleaseError("local release image identity invalid")
    return f"drycontroller.local/{service}:sha256-{image_id.removeprefix('sha256:')}"


def canonical_topology_digest(compose_digest: str, service_inventory: list[str]) -> str:
    topology = {
        "compose_digest": compose_digest,
        "health_contract": HOST_HEALTH_CONTRACT,
        "service_inventory": service_inventory,
    }
    return f"sha256:{hashlib.sha256(canonical_manifest_bytes(topology)).hexdigest()}"


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ReleaseError(f"manifest {key} must be a non-empty string")
    return value


def _canonical_uuid(data: dict[str, Any], key: str) -> str:
    value = _text(data, key)
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ReleaseError(f"{key} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ReleaseError(f"{key} must be a canonical UUID")
    return value


def validate_manifest(manifest: object) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        raise ReleaseError("manifest fields do not match the canonical schema")
    if manifest.get("schema") != SOFTWARE_RELEASE_SCHEMA:
        raise ReleaseError("unsupported software release schema")

    _canonical_uuid(manifest, "release_id")
    _canonical_uuid(manifest, "host_release_id")
    if not _DIGEST.fullmatch(_text(manifest, "topology_digest")):
        raise ReleaseError("manifest topology_digest must be a sha256 digest")

    sequence = manifest.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ReleaseError("manifest sequence must be a positive integer")
    if not _SEMVER.fullmatch(_text(manifest, "version")):
        raise ReleaseError("manifest version must be semver")
    if not _COMMIT.fullmatch(_text(manifest, "source_commit")):
        raise ReleaseError("manifest source_commit must be a lowercase 40-hex commit")
    registry = _text(manifest, "registry")
    if not _REGISTRY.fullmatch(registry):
        raise ReleaseError("manifest registry must be one host with optional port")
    if not _DIGEST.fullmatch(_text(manifest, "signing_key_sha256")):
        raise ReleaseError("manifest signing_key_sha256 must be a sha256 digest")
    if manifest.get("cosign") != COSIGN_TOOLCHAIN:
        raise ReleaseError("manifest cosign toolchain is not approved")

    services = manifest.get("services")
    if not isinstance(services, dict) or not services or len(services) > 128:
        raise ReleaseError("manifest must contain a bounded service set")
    if not all(isinstance(service, str) and _SERVICE_NAME.fullmatch(service) for service in services):
        raise ReleaseError("manifest service name invalid")
    image_ids = manifest.get("image_ids")
    if not isinstance(image_ids, dict) or set(image_ids) != set(services):
        raise ReleaseError("manifest image ID set must match the service set")
    for service in sorted(services):
        reference = services.get(service)
        if not isinstance(reference, str) or "@" not in reference:
            raise ReleaseError(f"service {service} image must be digest locked")
        repository, digest = reference.rsplit("@", 1)
        prefix = f"{registry}/"
        if not repository.startswith(prefix) or not _REPOSITORY_PATH.fullmatch(repository[len(prefix) :]):
            raise ReleaseError(f"service {service} image must use the manifest registry")
        if not _DIGEST.fullmatch(digest):
            raise ReleaseError(f"service {service} image must be digest locked")
        image_id = image_ids.get(service)
        if not isinstance(image_id, str) or not _DIGEST.fullmatch(image_id):
            raise ReleaseError(f"service {service} image ID must be digest locked")
    return manifest


def validate_host_active(host_active: object, compose_bytes: bytes) -> dict[str, Any]:
    if not isinstance(host_active, dict) or set(host_active) != _HOST_ACTIVE_FIELDS:
        raise ReleaseError("active HostRelease fields invalid")
    if host_active.get("schema") != HOST_ACTIVE_SCHEMA:
        raise ReleaseError("active HostRelease schema invalid")
    _canonical_uuid(host_active, "host_release_id")
    for key in ("host_release_manifest_digest", "topology_digest", "compose_digest"):
        if not _DIGEST.fullmatch(_text(host_active, key)):
            raise ReleaseError(f"active HostRelease {key} must be a sha256 digest")
    inventory = host_active.get("service_inventory")
    if (
        not isinstance(inventory, list)
        or not inventory
        or len(inventory) > 128
        or not all(isinstance(service, str) and _SERVICE_NAME.fullmatch(service) for service in inventory)
        or inventory != sorted(set(inventory))
    ):
        raise ReleaseError("active HostRelease service inventory invalid")
    actual_compose_digest = f"sha256:{hashlib.sha256(compose_bytes).hexdigest()}"
    if host_active["compose_digest"] != actual_compose_digest:
        raise ReleaseError("active HostRelease Compose digest mismatch")
    if host_active["topology_digest"] != canonical_topology_digest(actual_compose_digest, inventory):
        raise ReleaseError("active HostRelease topology digest mismatch")
    return host_active


def assert_software_release_host_binding(
    manifest: dict[str, Any],
    host_active: dict[str, Any],
) -> None:
    if (
        manifest["host_release_id"] != host_active["host_release_id"]
        or manifest["topology_digest"] != host_active["topology_digest"]
    ):
        raise ReleaseError("SoftwareRelease HostRelease binding mismatch")
    if sorted(manifest["services"]) != host_active["service_inventory"]:
        raise ReleaseError("SoftwareRelease service inventory does not match active topology")


def _read_json(path: Path) -> Any:
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            raise ReleaseError(f"JSON file too large: {path.name}")
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReleaseError(f"required file missing: {path.name}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"invalid JSON file: {path.name}") from exc


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(f"{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def validate_release_slot(slot: object, *, require_verified: bool) -> dict[str, Any]:
    if not isinstance(slot, dict) or set(slot) != {
        "manifest",
        "manifest_digest",
        "manifest_signature",
        "verified_at",
    }:
        raise ReleaseError("release slot invalid")
    manifest = validate_manifest(slot.get("manifest"))
    digest = slot.get("manifest_digest")
    signature = slot.get("manifest_signature")
    if digest != canonical_manifest_digest(manifest):
        raise ReleaseError("release slot manifest digest mismatch")
    if not isinstance(signature, str) or not signature:
        raise ReleaseError("release slot signature missing")
    if require_verified and not isinstance(slot.get("verified_at"), str):
        raise ReleaseError("release slot verification evidence missing")
    return slot


def validate_release_journal(journal: object) -> dict[str, Any]:
    if (
        not isinstance(journal, dict)
        or set(journal) != _JOURNAL_FIELDS
        or journal.get("schema") != RELEASE_JOURNAL_SCHEMA
        or not isinstance(journal.get("phase"), str)
    ):
        raise ReleaseError("release journal invalid")
    if journal.get("active") is None:
        raise ReleaseError("active release missing")
    last_operation_id = journal.get("last_operation_id")
    if last_operation_id is not None and (
        not isinstance(last_operation_id, str) or _OPERATION_ID.fullmatch(last_operation_id) is None
    ):
        raise ReleaseError("release journal last operation invalid")
    validate_release_slot(journal["active"], require_verified=True)
    if journal.get("candidate") is not None:
        validate_release_slot(journal["candidate"], require_verified=False)
    if journal.get("previous") is not None:
        validate_release_slot(journal["previous"], require_verified=True)
    return journal


class ReleaseEngine:
    def __init__(
        self,
        *,
        paths: ReleasePaths | None = None,
        command_runner: Callable[[list[str]], str] | None = None,
        runtime_command_runner: Callable[[list[str], float], str] | None = None,
        policy_fetcher: Callable[[str], dict[str, Any]] | None = None,
        health_checker: Callable[[str], None] | None = None,
        runtime_health_checker: Callable[[str, float], None] | None = None,
        image_set_verifier: Callable[[dict[str, str]], dict[str, str]] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        runtime_ready_timeout: float = _RUNTIME_READY_TIMEOUT_SECONDS,
        runtime_ready_poll: float = _RUNTIME_READY_POLL_SECONDS,
    ) -> None:
        self.paths = paths or ReleasePaths.production()
        self._command_runner = command_runner or self._run_command
        self._runtime_command_runner = runtime_command_runner or (
            (lambda args, timeout: self._run_command(args, timeout=timeout))
            if command_runner is None
            else (lambda args, _timeout: command_runner(args))
        )
        self._policy_fetcher = policy_fetcher or self._fetch_policy
        self._runtime_health_checker = runtime_health_checker or (
            (lambda url, timeout: self._check_health(url, timeout=timeout))
            if health_checker is None
            else (lambda url, _timeout: health_checker(url))
        )
        self._image_set_verifier = image_set_verifier or (
            self._verify_portable_image_set if command_runner is None else self._verify_inspected_image_set
        )
        self._monotonic = monotonic or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._runtime_ready_timeout = runtime_ready_timeout
        self._runtime_ready_poll = runtime_ready_poll

    @contextlib.contextmanager
    def _transaction_lock(self) -> Iterator[None]:
        try:
            self.paths.lock.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.paths.lock,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
        except OSError as exc:
            raise ReleaseError("release transaction lock unavailable") from exc
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as exc:
            os.close(descriptor)
            raise ReleaseError("release transaction lock failed") from exc
        try:
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _run_command(self, args: list[str], *, timeout: float | None = None) -> str:
        uses_docker = False
        if args and args[0] == _DOCKER:
            uses_docker = True
        elif args and args[0] == _DOCKER_COMPOSE:
            uses_docker = True
        elif args and Path(args[0]).name in {"docker", "docker-compose"}:
            raise ReleaseError("Docker executable path drift")
        try:
            environment = (
                _docker_environment(registry_network=_requires_registry_auth(args))
                if uses_docker or _requires_registry_auth(args)
                else contextlib.nullcontext(_FIXED_ENV)
            )
            daemon = _bound_docker_daemon() if uses_docker else contextlib.nullcontext(None)
            with environment as command_env, daemon as binding, _opened_command(args) as (
                command_args,
                inherited_fds,
            ):
                if binding is not None:
                    docker_host, socket_descriptor = binding
                    command_env = {**command_env, "DOCKER_HOST": docker_host}
                    inherited_fds = (*inherited_fds, socket_descriptor)
                completed = subprocess.run(
                    command_args,
                    cwd=self.paths.project,
                    env=command_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                    timeout=timeout,
                    pass_fds=inherited_fds,
                )
        except subprocess.TimeoutExpired as exc:
            raise ReleaseError(f"command timed out: {Path(args[0]).name}") from exc
        except OSError as exc:
            raise ReleaseError(f"command unavailable: {Path(args[0]).name}") from exc
        if completed.returncode != 0:
            action = args[1] if len(args) > 1 else Path(args[0]).name
            raise ReleaseError(f"command failed: {Path(args[0]).name} {action}")
        return completed.stdout

    def _check_health(self, url: str, *, timeout: float = 5) -> None:
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=min(5, timeout)) as response:
                if response.status != 200:
                    raise ReleaseError("release health check failed")
        except (OSError, urllib.error.URLError) as exc:
            raise ReleaseError("release health check failed") from exc

    def _cloud_credentials(self) -> tuple[str, str]:
        wanted = {"CLOUD_OPS_BASE_URL"}
        values: dict[str, str] = {}
        try:
            lines = self.paths.cloud_config.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise ReleaseError("cloud policy configuration unavailable") from exc
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            if key not in wanted:
                continue
            if key in values:
                raise ReleaseError("duplicate cloud policy configuration")
            try:
                tokens = shlex.split(raw_value, posix=True)
            except ValueError as exc:
                raise ReleaseError("invalid cloud policy configuration") from exc
            if len(tokens) != 1 or not tokens[0]:
                raise ReleaseError("invalid cloud policy configuration")
            values[key] = tokens[0]
        if set(values) != wanted or not values["CLOUD_OPS_BASE_URL"].startswith("https://"):
            raise ReleaseError("cloud policy configuration incomplete")
        try:
            stat = self.paths.device.stat()
        except OSError as exc:
            raise ReleaseError("device identity unavailable") from exc
        if stat.st_mode & 0o077 or (os.geteuid() == 0 and stat.st_uid != 0):
            raise ReleaseError("device identity must be root-owned 0600")
        identity = _read_json(self.paths.device)
        if not isinstance(identity, dict) or identity.get("schema") != "drycontroller.device-identity.v1":
            raise ReleaseError("device identity invalid")
        credential = identity.get("runtime_credential")
        credential_hash = identity.get("runtime_credential_hash")
        if (
            not isinstance(credential, str)
            or not credential
            or not isinstance(credential_hash, str)
            or not hmac.compare_digest(hashlib.sha256(credential.encode("utf-8")).hexdigest(), credential_hash)
        ):
            raise ReleaseError("device identity invalid")
        try:
            expires_at = dt.datetime.fromisoformat(str(identity["credential_expires_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError) as exc:
            raise ReleaseError("device credential expiry invalid") from exc
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=dt.timezone.utc)
        if expires_at.astimezone(dt.timezone.utc) <= dt.datetime.now(dt.timezone.utc):
            raise ReleaseError("device credential expired")
        return values["CLOUD_OPS_BASE_URL"].rstrip("/"), credential

    def _fetch_policy(self, operation_id: str) -> dict[str, Any]:
        base_url, token = self._cloud_credentials()
        request = urllib.request.Request(
            f"{base_url}/api/v1/ota/release-policy/{operation_id}",
            headers={"X-Device-Credential": token, "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > _MAX_JSON_BYTES:
                    raise ReleaseError("release policy response too large")
                body = response.read(_MAX_JSON_BYTES + 1)
                if len(body) > _MAX_JSON_BYTES:
                    raise ReleaseError("release policy response too large")
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise ReleaseError("release policy fetch failed") from exc
        try:
            policy = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("release policy response invalid") from exc
        if not isinstance(policy, dict):
            raise ReleaseError("release policy response invalid")
        return policy

    def _load_journal(self) -> dict[str, Any]:
        return validate_release_journal(_read_json(self.paths.journal))

    def _persist_journal(self, journal: dict[str, Any]) -> None:
        _atomic_write(
            self.paths.journal,
            json.dumps(journal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )

    def _load_host_active(self) -> dict[str, Any]:
        try:
            compose_bytes = self.paths.compose.read_bytes()
        except OSError as exc:
            raise ReleaseError("active HostRelease Compose unavailable") from exc
        return validate_host_active(_read_json(self.paths.host_active), compose_bytes)

    def _assert_host_binding(
        self,
        manifest: dict[str, Any],
        host_active: dict[str, Any] | None = None,
    ) -> None:
        assert_software_release_host_binding(
            manifest,
            host_active if host_active is not None else self._load_host_active(),
        )

    def _read_operation_id(self) -> str:
        request = _read_json(self.paths.request)
        if not isinstance(request, dict) or set(request) != {"operation_id"}:
            raise ReleaseError("release request must contain only operation_id")
        operation_id = request.get("operation_id")
        if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
            raise ReleaseError("release request operation_id invalid")
        return operation_id

    @staticmethod
    def _slot(manifest: dict[str, Any], digest: str, signature: str, *, verified_at: str | None) -> dict[str, Any]:
        return {
            "manifest": manifest,
            "manifest_digest": digest,
            "manifest_signature": signature,
            "verified_at": verified_at,
        }

    def _validate_slot(self, slot: object, *, require_verified: bool) -> dict[str, Any]:
        return validate_release_slot(slot, require_verified=require_verified)

    def initialize_active(self, seed: object) -> dict[str, Any]:
        with self._transaction_lock():
            return self._initialize_active(seed)

    def initialize_active_with_lock_fd(self, seed: object, descriptor: int) -> dict[str, Any]:
        self._validate_inherited_lock(descriptor)
        return self._initialize_active(seed)

    def _validate_inherited_lock(self, descriptor: int) -> None:
        try:
            opened = os.fstat(descriptor)
            expected = self.paths.lock.lstat()
        except OSError as exc:
            raise ReleaseError("inherited release lock descriptor invalid") from exc
        if (
            not stat.S_ISREG(expected.st_mode)
            or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise ReleaseError("inherited release lock descriptor does not match release lock")
        if sys.platform.startswith("linux"):
            try:
                fields = [
                    line.split()
                    for line in Path(f"/proc/self/fdinfo/{descriptor}").read_text(
                        encoding="utf-8"
                    ).splitlines()
                    if line.startswith("lock:")
                ]
            except OSError as exc:
                raise ReleaseError("inherited release lock descriptor invalid") from exc
            if not any(parts[2:5] == ["FLOCK", "ADVISORY", "WRITE"] for parts in fields):
                raise ReleaseError("inherited release lock descriptor is not held")
            return
        try:
            probe = os.open(
                self.paths.lock,
                os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
        except OSError as exc:
            raise ReleaseError("inherited release lock descriptor invalid") from exc
        try:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            fcntl.flock(probe, fcntl.LOCK_UN)
            raise ReleaseError("inherited release lock descriptor is not held")
        finally:
            os.close(probe)

    def _initialize_active(self, seed: object) -> dict[str, Any]:
        if not isinstance(seed, dict) or set(seed) != {
            "manifest",
            "manifest_digest",
            "manifest_signature",
        }:
            raise ReleaseError("initial release seed fields invalid")
        manifest = validate_manifest(seed.get("manifest"))
        self._assert_host_binding(manifest)
        digest = canonical_manifest_digest(manifest)
        if seed.get("manifest_digest") != digest:
            raise ReleaseError("initial release seed digest mismatch")
        signature = seed.get("manifest_signature")
        if not isinstance(signature, str) or not signature:
            raise ReleaseError("initial release seed signature missing")
        slot = self._slot(manifest, digest, signature, verified_at=None)

        expected_existing = {
            "schema": RELEASE_JOURNAL_SCHEMA,
            "phase": "idle",
            "operation_id": None,
            "last_operation_id": None,
            "active": None,
            "candidate": None,
            "previous": None,
            "effect": None,
            "error": None,
        }
        already_initialized = self.paths.journal.exists()
        if already_initialized:
            journal = self._load_journal()
            existing_shape = {**journal, "active": None}
            active = self._validate_slot(journal["active"], require_verified=True)
            if existing_shape != expected_existing or any(
                active[key] != slot[key]
                for key in ("manifest", "manifest_digest", "manifest_signature")
            ):
                raise ReleaseError("initial release conflicts with existing journal")

        try:
            self._materialize_and_verify_manifest(slot)
            self._record_local_images(slot)
        except ReleaseError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed on verifier boundary faults
            raise ReleaseError("initial release verification failed") from exc

        if not already_initialized:
            slot["verified_at"] = utc_now()
            expected_existing["active"] = slot
            self._persist_journal(expected_existing)
        return {
            "release_id": manifest["release_id"],
            "manifest_digest": digest,
            "status": "already_initialized" if already_initialized else "initialized",
        }

    def start_active(self) -> dict[str, Any]:
        with self._transaction_lock():
            return self._start_active()

    def start_active_with_lock_fd(self, descriptor: int) -> dict[str, Any]:
        self._validate_inherited_lock(descriptor)
        return self._start_active()

    def _start_active(self) -> dict[str, Any]:
        journal = self._load_journal()
        if journal["phase"] != "idle":
            self._recover_interrupted(journal)
        active = self._validate_slot(journal["active"], require_verified=True)
        self._assert_host_binding(active["manifest"])
        journal.update({"phase": "active_starting", "effect": None, "error": None})
        self._persist_journal(journal)
        try:
            override = self._effect(
                journal,
                "active_manifest_verify",
                lambda: self._materialize_and_verify_manifest(active),
            )
            self._prove_local_images(journal, active, "active")
            self._effect(
                journal,
                "active_compose_up",
                lambda: self._command_runner(
                    self._compose_args(
                        override, "up", "-d", "--remove-orphans", "--pull", "never", "--no-build"
                    )
                ),
            )
            journal["phase"] = "active_health_check"
            self._persist_journal(journal)
            self._verify_runtime(journal, active, override, "active")
        except ReleaseError as exc:
            journal.update({"phase": "idle", "effect": None, "error": str(exc)[:512]})
            self._persist_journal(journal)
            raise
        journal.update({"phase": "idle", "effect": None, "error": None})
        self._persist_journal(journal)
        return {
            "release_id": active["manifest"]["release_id"],
            "manifest_digest": active["manifest_digest"],
            "status": "running",
        }

    def _validate_policy(self, policy: object, operation_id: str, active: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(policy, dict) or set(policy) != _POLICY_FIELDS:
            raise ReleaseError("release policy fields invalid")
        if policy.get("operation_id") != operation_id:
            raise ReleaseError("release policy operation binding mismatch")
        node_id = policy.get("node_id")
        if isinstance(node_id, bool) or not isinstance(node_id, int) or node_id != int(operation_id[16:], 16):
            raise ReleaseError("release policy node binding mismatch")

        device = _read_json(self.paths.device)
        if not isinstance(device, dict):
            raise ReleaseError("device identity invalid")
        for policy_key, device_key in (
            ("tenant_id", "tenant_id"),
            ("line_id", "line_id"),
            ("device_id", "device_id"),
            ("node_name", "node_name"),
        ):
            if policy.get(policy_key) != device.get(device_key):
                raise ReleaseError("policy device binding mismatch")

        manifest = validate_manifest(policy.get("manifest"))
        digest = canonical_manifest_digest(manifest)
        if (
            policy.get("release_id") != manifest.get("release_id")
            or policy.get("manifest_digest") != digest
        ):
            raise ReleaseError("release policy manifest binding mismatch")
        signature = policy.get("manifest_signature")
        if not isinstance(signature, str) or not signature:
            raise ReleaseError("release policy signature missing")
        _, registry_status = _read_registry_auth_document(self.paths.registry_status)
        if (
            not isinstance(registry_status, dict)
            or registry_status.get("status") != "active"
            or registry_status.get("registry_host") != manifest.get("registry")
        ):
            raise ReleaseError("release registry binding invalid")
        return self._slot(manifest, digest, signature, verified_at=None)

    def _release_directory(self, slot: dict[str, Any]) -> Path:
        digest = str(slot["manifest_digest"])
        if not _DIGEST.fullmatch(digest):
            raise ReleaseError("release slot digest invalid")
        directory = self.paths.releases / digest.removeprefix("sha256:")
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        return directory

    def _verify_key_binding(self, manifest: dict[str, Any]) -> None:
        try:
            key = self.paths.cosign_pubkey.read_bytes()
        except OSError as exc:
            raise ReleaseError("cosign public key unavailable") from exc
        actual = f"sha256:{hashlib.sha256(key).hexdigest()}"
        if actual != manifest.get("signing_key_sha256"):
            raise ReleaseError("cosign public key digest mismatch")

    def _materialize_and_verify_manifest(self, slot: dict[str, Any]) -> Path:
        slot = self._validate_slot(slot, require_verified=False)
        manifest = slot["manifest"]
        self._assert_host_binding(manifest)
        self._verify_key_binding(manifest)
        try:
            signature = base64.b64decode(slot["manifest_signature"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ReleaseError("release manifest signature encoding invalid") from exc
        if not signature or len(signature) > 16 * 1024:
            raise ReleaseError("release manifest signature size invalid")
        directory = self._release_directory(slot)
        manifest_path = directory / "manifest.json"
        signature_path = directory / "manifest.sigstore.json"
        override_path = directory / "compose-images.yml"
        _atomic_write(manifest_path, canonical_manifest_bytes(manifest))
        _atomic_write(signature_path, signature)
        override_lines = ["services:"]
        for service in sorted(manifest["services"]):
            override_lines.extend(
                [
                    f"  {service}:",
                    f"    image: {json.dumps(local_image_reference(service, manifest['image_ids'][service]))}",
                ]
            )
        _atomic_write(override_path, ("\n".join(override_lines) + "\n").encode("utf-8"))
        self._command_runner(
            [
                _COSIGN,
                "verify-blob",
                "--key",
                str(self.paths.cosign_pubkey),
                "--insecure-ignore-tlog=true",
                "--bundle",
                str(signature_path),
                str(manifest_path),
            ]
        )
        self._command_runner(self._compose_args(override_path, "config", "--quiet"))
        return override_path

    def _compose_args(self, override: Path, *args: str) -> list[str]:
        return [
            _DOCKER_COMPOSE,
            "--project-name",
            "drycontroller-edge",
            "--env-file",
            "/etc/drycontroller/runtime.env",
            "--file",
            str(self.paths.compose),
            "--file",
            str(override),
            *args,
        ]

    def _effect(
        self,
        journal: dict[str, Any],
        name: str,
        action: Callable[[], Any],
    ) -> Any:
        journal["effect"] = {"name": name, "status": "started"}
        self._persist_journal(journal)
        try:
            result = action()
        except Exception as exc:  # noqa: BLE001 - persist unexpected effect failures before conversion
            journal["effect"] = {"name": name, "status": "failed"}
            journal["error"] = str(exc)[:512]
            self._persist_journal(journal)
            if isinstance(exc, ReleaseError):
                raise
            raise ReleaseError(f"release effect failed: {name}") from exc
        journal["effect"] = {"name": name, "status": "succeeded"}
        self._persist_journal(journal)
        return result

    def _verify_inspected_image_set(self, images: dict[str, str]) -> dict[str, str]:
        runtime_ids: dict[str, str] = {}
        for reference, expected_image_id in images.items():
            output = self._command_runner(
                [_DOCKER, "image", "inspect", "--format", "{{.Id}}", reference]
            ).strip()
            if output != expected_image_id:
                raise ReleaseError("release image ID mismatch")
            runtime_ids[reference] = output
        return runtime_ids

    def _verify_portable_image_set(self, images: dict[str, str]) -> dict[str, str]:
        self.paths.releases.mkdir(parents=True, exist_ok=True)
        archive = self.paths.releases / ".local-image-verification.tar"
        try:
            self._command_runner([_DOCKER, "image", "save", "-o", str(archive), *images])
            actual = docker_archive_config_digests(archive)
        finally:
            archive.unlink(missing_ok=True)
        if any(actual.get(reference) != image_id for reference, image_id in images.items()):
            raise ReleaseError("release image ID mismatch")
        return {
            reference: self._command_runner(
                [_DOCKER, "image", "inspect", "--format", "{{.Id}}", reference]
            ).strip()
            for reference in images
        }

    def _record_local_images(self, slot: dict[str, Any]) -> None:
        manifest = validate_manifest(slot["manifest"])
        expected = {
            local_image_reference(service, manifest["image_ids"][service]): manifest["image_ids"][service]
            for service in sorted(manifest["services"])
        }
        runtime_ids = self._image_set_verifier(expected)
        if set(runtime_ids) != set(expected) or not all(
            isinstance(runtime_id, str) and _DIGEST.fullmatch(runtime_id)
            for runtime_id in runtime_ids.values()
        ):
            raise ReleaseError("local release image evidence invalid")
        evidence = {
            "manifest_digest": slot["manifest_digest"],
            "images": {
                service: {
                    "reference": local_image_reference(service, manifest["image_ids"][service]),
                    "image_id": manifest["image_ids"][service],
                    "runtime_id": runtime_ids[
                        local_image_reference(service, manifest["image_ids"][service])
                    ],
                }
                for service in sorted(manifest["services"])
            },
        }
        _atomic_write(
            self._release_directory(slot) / "local-images.json",
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )

    def _prove_local_images(self, journal: dict[str, Any], slot: dict[str, Any], prefix: str) -> None:
        manifest = validate_manifest(slot["manifest"])
        evidence = _read_json(self._release_directory(slot) / "local-images.json")
        if (
            not isinstance(evidence, dict)
            or evidence.get("manifest_digest") != slot["manifest_digest"]
            or not isinstance(evidence.get("images"), dict)
            or set(evidence["images"]) != set(manifest["services"])
        ):
            raise ReleaseError("local release image evidence invalid")
        for service in sorted(manifest["services"]):
            image_id = manifest["image_ids"][service]
            reference = local_image_reference(service, image_id)
            record = evidence["images"].get(service)
            if (
                not isinstance(record, dict)
                or record.get("reference") != reference
                or record.get("image_id") != image_id
                or not isinstance(record.get("runtime_id"), str)
                or _DIGEST.fullmatch(record["runtime_id"]) is None
            ):
                raise ReleaseError("local release image evidence invalid")
            self._effect(
                journal,
                f"{prefix}_inspect_{service}",
                lambda reference=reference, runtime_id=record["runtime_id"]: self._assert_runtime_image(
                    reference, runtime_id
                ),
            )

    def _assert_runtime_image(self, reference: str, runtime_id: str) -> None:
        output = self._command_runner(
            [_DOCKER, "image", "inspect", "--format", "{{.Id}}", reference]
        )
        if output.strip() != runtime_id:
            raise ReleaseError("local release image missing")

    def _pull_and_verify_candidate(self, journal: dict[str, Any], slot: dict[str, Any]) -> None:
        manifest = validate_manifest(slot["manifest"])
        for service in sorted(manifest["services"]):
            reference = manifest["services"][service]
            self._effect(
                journal,
                f"candidate_pull_{service}",
                lambda reference=reference: self._command_runner([_DOCKER, "pull", reference]),
            )
            self._effect(
                journal,
                f"candidate_cosign_{service}",
                lambda reference=reference: self._command_runner(
                    [
                        _COSIGN,
                        "verify",
                        "--insecure-ignore-tlog=true",
                        "--key",
                        str(self.paths.cosign_pubkey),
                        reference,
                    ]
                ),
            )
            self._effect(
                journal,
                f"candidate_local_tag_{service}",
                lambda reference=reference, service=service: self._command_runner(
                    [
                        _DOCKER,
                        "tag",
                        reference,
                        local_image_reference(service, manifest["image_ids"][service]),
                    ]
                ),
            )
        self._effect(journal, "candidate_image_ids", lambda: self._record_local_images(slot))
        self._prove_local_images(journal, slot, "candidate")

    @staticmethod
    def _parse_compose_ps(output: str) -> list[dict[str, Any]]:
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            try:
                parsed = [json.loads(line) for line in output.splitlines() if line.strip()]
            except json.JSONDecodeError as exc:
                raise ReleaseError("compose status output invalid") from exc
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
            raise ReleaseError("compose status output invalid")
        return parsed

    def _verify_runtime(self, journal: dict[str, Any], slot: dict[str, Any], override: Path, prefix: str) -> None:
        manifest = validate_manifest(slot["manifest"])

        deadline = self._monotonic() + self._runtime_ready_timeout

        def remaining() -> float:
            timeout = deadline - self._monotonic()
            if timeout <= 0:
                raise ReleaseError("release runtime readiness deadline exceeded")
            return timeout

        def compose_status() -> bool:
            output = self._runtime_command_runner(
                self._compose_args(override, "ps", "--all", "--format", "json"), remaining()
            )
            records = {str(record.get("Service") or ""): record for record in self._parse_compose_ps(output)}
            services = sorted(manifest["services"])
            if set(records) != set(services):
                raise ReleaseError("compose service inventory mismatch")
            pending = False
            for service in services:
                record = records[service]
                if record.get("Image") != local_image_reference(service, manifest["image_ids"][service]):
                    raise ReleaseError("running image does not match release manifest")
                state = str(record.get("State") or "").lower()
                if service == "mosquitto-init":
                    if state in {"created", "running", "restarting"}:
                        pending = True
                    elif state != "exited" or int(record.get("ExitCode") or 0) != 0:
                        raise ReleaseError("mosquitto-init did not complete successfully")
                elif state != "running":
                    if state in {"created", "restarting"}:
                        pending = True
                    else:
                        raise ReleaseError("release service is not running")
                health = str(record.get("Health") or "").lower()
                if service != "mosquitto-init" and health in {"starting", "unhealthy"}:
                    pending = True
                elif service != "mosquitto-init" and health not in {"", "healthy"}:
                    raise ReleaseError("release service is unhealthy")
            return not pending

        def runtime_ready() -> None:
            last_error: ReleaseError | None = None
            while True:
                if compose_status():
                    try:
                        for url in _HEALTH_URLS:
                            self._runtime_health_checker(url, remaining())
                    except ReleaseError as exc:
                        last_error = exc
                    else:
                        remaining()
                        return
                try:
                    self._sleeper(min(self._runtime_ready_poll, remaining()))
                except ReleaseError as exc:
                    raise ReleaseError("release runtime readiness deadline exceeded") from (last_error or exc)

        self._effect(journal, f"{prefix}_runtime_ready", runtime_ready)

    def _clear_without_mutation(
        self,
        journal: dict[str, Any],
        original_previous: dict[str, Any] | None,
        error: ReleaseError,
    ) -> None:
        journal.update(
            {
                "phase": "idle",
                "operation_id": None,
                "candidate": None,
                "previous": original_previous,
                "effect": None,
                "error": str(error)[:512],
            }
        )
        self._persist_journal(journal)

    def _rollback(self, journal: dict[str, Any], reason: str) -> None:
        previous = self._validate_slot(journal.get("previous"), require_verified=True)
        self._assert_host_binding(previous["manifest"])
        journal["phase"] = "rollback_started"
        journal["error"] = reason[:512]
        self._persist_journal(journal)
        try:
            override = self._materialize_and_verify_manifest(previous)
            self._effect(
                journal,
                "previous_compose_up",
                lambda: self._command_runner(
                    self._compose_args(
                        override, "up", "-d", "--remove-orphans", "--pull", "never", "--no-build"
                    )
                ),
            )
            journal["phase"] = "rollback_applied"
            self._persist_journal(journal)
            self._verify_runtime(journal, previous, override, "previous")
        except ReleaseError as exc:
            journal["phase"] = "rollback_failed"
            journal["error"] = f"rollback_failed:{exc}"[:512]
            self._persist_journal(journal)
            raise ReleaseError(f"rollback failed: {exc}") from exc
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
        self._persist_journal(journal)

    def _recover_interrupted(self, journal: dict[str, Any]) -> None:
        phase = str(journal.get("phase") or "")
        mutation_phases = {
            "candidate_apply_started",
            "candidate_apply_succeeded",
            "candidate_health_check",
            "promotion_started",
            "rollback_started",
            "rollback_applied",
            "rollback_failed",
        }
        if phase in mutation_phases:
            if journal.get("previous") is None:
                raise ReleaseError("interrupted release has no previous slot")
            self._rollback(journal, "interrupted_release_recovered")
        else:
            journal.update(
                {
                    "phase": "idle",
                    "operation_id": None,
                    "candidate": None,
                    "effect": None,
                    "error": "interrupted_release_recovered",
                }
            )
            self._persist_journal(journal)
        raise ReleaseError("interrupted_release_recovered")

    def apply(self) -> dict[str, Any]:
        with self._transaction_lock():
            return self._apply()

    def _apply(self) -> dict[str, Any]:
        operation_id = self._read_operation_id()
        journal = self._load_journal()
        if journal["phase"] != "idle":
            self._recover_interrupted(journal)

        active = self._validate_slot(journal["active"], require_verified=True)
        host_active = self._load_host_active()
        self._assert_host_binding(active["manifest"], host_active)
        original_previous = journal.get("previous")
        policy = self._policy_fetcher(operation_id)
        candidate = self._validate_policy(policy, operation_id, active)
        self._assert_host_binding(candidate["manifest"], host_active)
        active_manifest = validate_manifest(active["manifest"])
        candidate_sequence = int(candidate["manifest"]["sequence"])
        active_sequence = int(active_manifest["sequence"])
        if candidate_sequence <= active_sequence:
            if (
                candidate_sequence == active_sequence
                and candidate["manifest_digest"] == active["manifest_digest"]
                and journal["last_operation_id"] == operation_id
            ):
                return {
                    "operation_id": operation_id,
                    "release_id": active_manifest["release_id"],
                    "manifest_digest": active["manifest_digest"],
                    "status": "succeeded",
                }
            raise ReleaseError("release sequence is not newer than active")
        journal.update(
            {
                "phase": "candidate_staging_started",
                "operation_id": operation_id,
                "candidate": candidate,
                "effect": None,
                "error": None,
            }
        )
        self._persist_journal(journal)

        mutation_started = False
        try:
            candidate_override = self._effect(
                journal,
                "candidate_manifest_verify",
                lambda: self._materialize_and_verify_manifest(candidate),
            )
            candidate["verified_at"] = utc_now()
            journal["candidate"] = candidate
            journal["phase"] = "candidate_verified"
            self._persist_journal(journal)

            journal["phase"] = "candidate_pulling"
            self._persist_journal(journal)
            self._pull_and_verify_candidate(journal, candidate)
            journal["phase"] = "candidate_pulled"
            self._persist_journal(journal)

            journal["phase"] = "previous_proving"
            self._persist_journal(journal)
            active_override = self._effect(
                journal,
                "active_manifest_verify",
                lambda: self._materialize_and_verify_manifest(active),
            )
            self._prove_local_images(journal, active, "active")
            self._effect(
                journal,
                "active_compose_config",
                lambda: self._command_runner(self._compose_args(active_override, "config", "--quiet")),
            )
            journal["previous"] = active
            journal["phase"] = "previous_proven"
            self._persist_journal(journal)

            mutation_started = True
            journal["phase"] = "candidate_apply_started"
            self._persist_journal(journal)
            self._effect(
                journal,
                "candidate_compose_up",
                lambda: self._command_runner(
                    self._compose_args(
                        candidate_override,
                        "up",
                        "-d",
                        "--remove-orphans",
                        "--pull",
                        "never",
                        "--no-build",
                    )
                ),
            )
            journal["phase"] = "candidate_apply_succeeded"
            self._persist_journal(journal)

            journal["phase"] = "candidate_health_check"
            self._persist_journal(journal)
            self._verify_runtime(journal, candidate, candidate_override, "candidate")
            journal["phase"] = "promotion_started"
            self._persist_journal(journal)
        except ReleaseError as exc:
            if mutation_started:
                self._rollback(journal, str(exc))
            else:
                self._clear_without_mutation(journal, original_previous, exc)
            raise

        journal.update(
            {
                "phase": "idle",
                "operation_id": None,
                "last_operation_id": operation_id,
                "active": candidate,
                "candidate": None,
                "previous": active,
                "effect": None,
                "error": None,
            }
        )
        self._persist_journal(journal)
        return {
            "operation_id": operation_id,
            "release_id": candidate["manifest"]["release_id"],
            "manifest_digest": candidate["manifest_digest"],
            "status": "succeeded",
        }


assert_engine_security_contract("software", Path(__file__))
