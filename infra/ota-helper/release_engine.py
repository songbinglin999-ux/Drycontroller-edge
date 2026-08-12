#!/usr/bin/python3
"""Signed, digest-locked Edge release transaction with deterministic rollback."""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


SOFTWARE_RELEASE_SCHEMA = "drycontroller.software-release.v1"
RELEASE_JOURNAL_SCHEMA = "drycontroller.release-journal.v1"
SOFTWARE_RELEASE_SERVICES = (
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
_LONG_RUNNING_SERVICES = set(SOFTWARE_RELEASE_SERVICES) - {"mosquitto-init"}
_MANIFEST_FIELDS = {
    "schema",
    "release_id",
    "sequence",
    "version",
    "source_commit",
    "registry",
    "signing_key_sha256",
    "services",
    "image_ids",
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
_HEALTH_URLS = (
    "http://127.0.0.1:8000/ready",
    "http://127.0.0.1:8082/ready",
)
_DOCKER = "/usr/bin/docker"
_COSIGN = "/usr/local/bin/cosign"
_MAX_JSON_BYTES = 1024 * 1024
_FIXED_ENV = {
    "HOME": "/root",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "DOCKER_CONFIG": "/etc/drycontroller/registry",
    "COMPOSE_PROJECT_NAME": "drycontroller-edge",
}


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleasePaths:
    journal: Path
    request: Path
    releases: Path
    project: Path
    compose: Path
    cosign_pubkey: Path
    registry_status: Path
    device: Path
    cloud_config: Path

    @classmethod
    def production(cls) -> "ReleasePaths":
        return cls(
            journal=Path("/var/lib/drycontroller-ota/release-journal.json"),
            request=Path("/var/lib/drycontroller-ota/request.json"),
            releases=Path("/var/lib/drycontroller-ota/releases"),
            project=Path("/opt/Drycontroller-edge"),
            compose=Path("/opt/Drycontroller-edge/docker-compose.yml"),
            cosign_pubkey=Path("/etc/drycontroller/keys/cosign.pub"),
            registry_status=Path("/etc/drycontroller/registry/status.json"),
            device=Path("/var/lib/drycontroller/device.json"),
            cloud_config=Path("/etc/drycontroller/tailnet.env"),
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


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ReleaseError(f"manifest {key} must be a non-empty string")
    return value


def validate_manifest(manifest: object) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        raise ReleaseError("manifest fields do not match the canonical schema")
    if manifest.get("schema") != SOFTWARE_RELEASE_SCHEMA:
        raise ReleaseError("unsupported software release schema")

    release_id = _text(manifest, "release_id")
    try:
        parsed_release_id = uuid.UUID(release_id)
    except ValueError as exc:
        raise ReleaseError("manifest release_id must be a canonical UUID") from exc
    if str(parsed_release_id) != release_id:
        raise ReleaseError("manifest release_id must be a canonical UUID")

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

    services = manifest.get("services")
    if not isinstance(services, dict) or set(services) != set(SOFTWARE_RELEASE_SERVICES):
        raise ReleaseError("manifest must contain the complete service set")
    image_ids = manifest.get("image_ids")
    if not isinstance(image_ids, dict) or set(image_ids) != set(SOFTWARE_RELEASE_SERVICES):
        raise ReleaseError("manifest must contain the complete image ID set")
    for service in SOFTWARE_RELEASE_SERVICES:
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


class ReleaseEngine:
    def __init__(
        self,
        *,
        paths: ReleasePaths | None = None,
        command_runner: Callable[[list[str]], str] | None = None,
        policy_fetcher: Callable[[str], dict[str, Any]] | None = None,
        health_checker: Callable[[str], None] | None = None,
    ) -> None:
        self.paths = paths or ReleasePaths.production()
        self._command_runner = command_runner or self._run_command
        self._policy_fetcher = policy_fetcher or self._fetch_policy
        self._health_checker = health_checker or self._check_health

    def _run_command(self, args: list[str]) -> str:
        try:
            completed = subprocess.run(
                args,
                cwd=self.paths.project,
                env=_FIXED_ENV,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise ReleaseError(f"command unavailable: {Path(args[0]).name}") from exc
        if completed.returncode != 0:
            action = args[1] if len(args) > 1 else Path(args[0]).name
            raise ReleaseError(f"command failed: {Path(args[0]).name} {action}")
        return completed.stdout

    def _check_health(self, url: str) -> None:
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status != 200:
                    raise ReleaseError("release health check failed")
        except (OSError, urllib.error.URLError) as exc:
            raise ReleaseError("release health check failed") from exc

    def _cloud_credentials(self) -> tuple[str, str]:
        wanted = {"CLOUD_OPS_BASE_URL", "CLOUD_OPS_ENROLL_TOKEN"}
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
        return values["CLOUD_OPS_BASE_URL"].rstrip("/"), values["CLOUD_OPS_ENROLL_TOKEN"]

    def _fetch_policy(self, operation_id: str) -> dict[str, Any]:
        base_url, token = self._cloud_credentials()
        request = urllib.request.Request(
            f"{base_url}/api/v1/ota/release-policy/{operation_id}",
            headers={"X-Enroll-Token": token, "Accept": "application/json"},
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
        journal = _read_json(self.paths.journal)
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
        self._validate_slot(journal["active"], require_verified=True)
        if journal.get("candidate") is not None:
            self._validate_slot(journal["candidate"], require_verified=False)
        if journal.get("previous") is not None:
            self._validate_slot(journal["previous"], require_verified=True)
        return journal

    def _persist_journal(self, journal: dict[str, Any]) -> None:
        _atomic_write(
            self.paths.journal,
            json.dumps(journal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
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

    def initialize_active(self, seed: object) -> dict[str, Any]:
        if not isinstance(seed, dict) or set(seed) != {
            "manifest",
            "manifest_digest",
            "manifest_signature",
        }:
            raise ReleaseError("initial release seed fields invalid")
        manifest = validate_manifest(seed.get("manifest"))
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
            for service in SOFTWARE_RELEASE_SERVICES:
                self._assert_local_image(manifest["image_ids"][service])
        except ReleaseError:
            raise
        except Exception as exc:
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
        journal = self._load_journal()
        if journal["phase"] != "idle":
            self._recover_interrupted(journal)
        active = self._validate_slot(journal["active"], require_verified=True)
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
                    self._compose_args(override, "up", "-d", "--remove-orphans", "--pull", "never")
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
        registry_status = _read_json(self.paths.registry_status)
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
        for service in SOFTWARE_RELEASE_SERVICES:
            override_lines.extend(
                [
                    f"  {service}:",
                    f"    image: {json.dumps(manifest['image_ids'][service])}",
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
            _DOCKER,
            "compose",
            "--project-name",
            "drycontroller-edge",
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
        except Exception as exc:
            journal["effect"] = {"name": name, "status": "failed"}
            journal["error"] = str(exc)[:512]
            self._persist_journal(journal)
            if isinstance(exc, ReleaseError):
                raise
            raise ReleaseError(f"release effect failed: {name}") from exc
        journal["effect"] = {"name": name, "status": "succeeded"}
        self._persist_journal(journal)
        return result

    def _assert_local_image(self, image_id: str) -> None:
        output = self._command_runner(
            [_DOCKER, "image", "inspect", "--format", "{{.Id}}", image_id]
        )
        if output.strip() != image_id:
            raise ReleaseError("local release image missing")

    def _assert_reference_image_id(self, reference: str, expected_image_id: str) -> None:
        output = self._command_runner(
            [_DOCKER, "image", "inspect", "--format", "{{.Id}}", reference]
        )
        if output.strip() != expected_image_id:
            raise ReleaseError("release image ID mismatch")

    def _prove_local_images(self, journal: dict[str, Any], slot: dict[str, Any], prefix: str) -> None:
        manifest = validate_manifest(slot["manifest"])
        for service in SOFTWARE_RELEASE_SERVICES:
            image_id = manifest["image_ids"][service]
            self._effect(
                journal,
                f"{prefix}_inspect_{service}",
                lambda image_id=image_id: self._assert_local_image(image_id),
            )

    def _pull_and_verify_candidate(self, journal: dict[str, Any], slot: dict[str, Any]) -> None:
        manifest = validate_manifest(slot["manifest"])
        for service in SOFTWARE_RELEASE_SERVICES:
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
            expected_image_id = manifest["image_ids"][service]
            self._effect(
                journal,
                f"candidate_image_id_{service}",
                lambda reference=reference, expected_image_id=expected_image_id: self._assert_reference_image_id(
                    reference, expected_image_id
                ),
            )
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

        def compose_status() -> None:
            output = self._command_runner(self._compose_args(override, "ps", "--all", "--format", "json"))
            records = {str(record.get("Service") or ""): record for record in self._parse_compose_ps(output)}
            if set(records) != set(SOFTWARE_RELEASE_SERVICES):
                raise ReleaseError("compose service inventory mismatch")
            for service in SOFTWARE_RELEASE_SERVICES:
                record = records[service]
                if record.get("Image") != manifest["image_ids"][service]:
                    raise ReleaseError("running image does not match release manifest")
                state = str(record.get("State") or "").lower()
                if service == "mosquitto-init":
                    if state != "exited" or int(record.get("ExitCode") or 0) != 0:
                        raise ReleaseError("mosquitto-init did not complete successfully")
                elif state != "running":
                    raise ReleaseError("release service is not running")
                health = str(record.get("Health") or "").lower()
                if service in _LONG_RUNNING_SERVICES and health not in {"", "healthy"}:
                    raise ReleaseError("release service is unhealthy")

        self._effect(journal, f"{prefix}_compose_status", compose_status)
        for index, url in enumerate(_HEALTH_URLS, start=1):
            self._effect(
                journal,
                f"{prefix}_health_{index}",
                lambda url=url: self._health_checker(url),
            )

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
        journal["phase"] = "rollback_started"
        journal["error"] = reason[:512]
        self._persist_journal(journal)
        try:
            override = self._materialize_and_verify_manifest(previous)
            self._effect(
                journal,
                "previous_compose_up",
                lambda: self._command_runner(
                    self._compose_args(override, "up", "-d", "--remove-orphans", "--pull", "never")
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
        operation_id = self._read_operation_id()
        journal = self._load_journal()
        if journal["phase"] != "idle":
            self._recover_interrupted(journal)

        active = self._validate_slot(journal["active"], require_verified=True)
        original_previous = journal.get("previous")
        policy = self._policy_fetcher(operation_id)
        candidate = self._validate_policy(policy, operation_id, active)
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
                    self._compose_args(candidate_override, "up", "-d", "--remove-orphans", "--pull", "never")
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
