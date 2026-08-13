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
import stat
import subprocess
import tarfile
import tempfile
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
HOST_HEALTH_CONTRACT = "drycontroller.edge-health.v1"
COSIGN_TOOLCHAIN = {
    "version": "v3.1.3",
    "linux_amd64_sha256": "sha256:4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71",
}
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
_COSIGN = "/usr/local/bin/cosign"
_IDENTITY_ROOT = Path("/opt/drycontroller-identity")
_IDENTITY_SIGNER = "/opt/drycontroller-identity/venv/bin/python"
_IDENTITY_MODULE = "identity_service.host_authorization"
_SYSTEMCTL = "/usr/bin/systemctl"
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
    "privileged",
    "pid",
    "ipc",
    "network_mode",
    "devices",
    "cap_add",
    "cap_drop",
    "security_opt",
}
_MAX_JSON_BYTES = 1024 * 1024
_MAX_LAYER_BYTES = 256 * 1024 * 1024
_FIXED_ENV = {
    "HOME": "/root",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "DOCKER_CONFIG": "/etc/drycontroller/registry",
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
            registry_status=Path("/etc/drycontroller/registry/status.json"),
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
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
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

    def _run_command(self, args: list[str], cwd: Path | None = None, input_bytes: bytes | None = None) -> str:
        inherited_fds: tuple[int, ...] = ()
        if len(args) == 3 and args[0].endswith("/start-active-release") and args[1] == "--lock-fd":
            try:
                inherited_fds = (int(args[2]),)
            except ValueError as exc:
                raise HostReleaseError("inherited release lock descriptor invalid") from exc
        try:
            completed = subprocess.run(
                args,
                cwd=cwd,
                env=_FIXED_ENV,
                stdin=subprocess.DEVNULL if input_bytes is None else None,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=30 * 60,
                pass_fds=inherited_fds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostReleaseError(f"command unavailable: {Path(args[0]).name}") from exc
        if completed.returncode != 0:
            raise HostReleaseError(f"command failed: {Path(args[0]).name}")
        try:
            return completed.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HostReleaseError("command output invalid") from exc

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
        registry_status = _read_json(self.paths.registry_status)
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
                    _DOCKER,
                    "compose",
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
        for service, definition in services.items():
            if not isinstance(definition, dict) or any(key in definition for key in _FORBIDDEN_COMPOSE_KEYS):
                raise HostReleaseError("HostRelease Compose contains forbidden privilege")
            image = definition.get("image")
            if image != _local_image_reference(service, software["image_ids"][service]):
                raise HostReleaseError("HostRelease Compose image is not the paired local image")
            for volume in definition.get("volumes") or []:
                source = volume.get("source") if isinstance(volume, dict) else None
                read_only = volume.get("read_only") is True if isinstance(volume, dict) else False
                if isinstance(source, str) and (source == "/var/run/docker.sock" or source.startswith("/run/docker")):
                    raise HostReleaseError("HostRelease Compose may not mount the Docker socket")
                if isinstance(source, str) and source.startswith("/"):
                    source_path = Path(source)
                    try:
                        relative = source_path.relative_to(rootfs.resolve()).as_posix()
                    except ValueError:
                        writable_exact = {
                            "/etc/drycontroller/license",
                            "/etc/drycontroller/registry",
                            "/var/lib/drycontroller/configs-runtime",
                            "/var/lib/drycontroller/discovery",
                        }
                        readonly_exact = {
                            "/etc/drycontroller/keys",
                            "/var/lib/drycontroller",
                            "/run/drycontroller-ota",
                            "/run/drycontroller-host-release",
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
            if not isinstance(record, dict) or record.get("image_id") != image_id:
                raise HostReleaseError("SoftwareRelease local image evidence invalid")
            actual = self._command_runner(
                [_DOCKER, "image", "inspect", "--format", "{{.Id}}", record["reference"]],
                None,
                None,
            ).strip()
            if actual != record.get("runtime_id") or record.get("image_id") != image_id:
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
        for link in (self.paths.current_link, self.paths.compatibility_link):
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
                _DOCKER,
                "compose",
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
