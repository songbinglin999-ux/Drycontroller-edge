#!/usr/bin/python3
"""Fixed two-service Edge application release transaction."""

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
from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Iterator

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from common.license_verifier import verify_installed_license  # noqa: E402


SOFTWARE_RELEASE_SCHEMA = "drycontroller.app-release.v2"
EDGE_RELEASE_PROFILE = "edge-app-v1"
RELEASE_JOURNAL_SCHEMA = "drycontroller.edge-app-release-journal.v2"
EDGE_SERVICES = ("edge-api", "edge-daq")
HEALTH_CONTRACT = {
    "edge-api": "http://127.0.0.1:18080/ready",
    "edge-daq": "http://127.0.0.1:8082/ready",
}
MANIFEST_FIELDS = {
    "schema",
    "profile",
    "release_id",
    "sequence",
    "source_commit",
    "deployment_digest",
    "images",
}
POLICY_FIELDS = {
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
SLOT_FIELDS = {"manifest", "manifest_digest", "manifest_signature", "verified_at"}
JOURNAL_FIELDS = {
    "schema",
    "phase",
    "operation_id",
    "last_operation_id",
    "last_result",
    "high_watermark",
    "active",
    "previous",
    "candidate",
    "error",
}
APPLY_RESULT_FIELDS = {
    "action",
    "operation_id",
    "status",
    "release_id",
    "manifest_digest",
    "sequence",
    "previous_release_id",
}
ROLLBACK_RESULT_FIELDS = APPLY_RESULT_FIELDS | {
    "expected_active_release_id",
    "target_release_id",
}
OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_REFERENCE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
COMPOSE_CONFIG_HASH = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON_BYTES = 1024 * 1024
HEALTH_TIMEOUT_SECONDS = 180.0
HEALTH_POLL_SECONDS = 2.0
HEALTH_ATTEMPT_TIMEOUT_SECONDS = 5.0
COMPOSE_BINARY = "/usr/libexec/docker/cli-plugins/docker-compose"
COMPOSE_PROJECT_NAME = "drycontroller-edge"
FIXED_ENV = {
    "HOME": "/root",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "DOCKER_HOST": "unix:///run/docker.sock",
    "COMPOSE_PROJECT_NAME": COMPOSE_PROJECT_NAME,
}
REGISTRY_AUTH_SCHEMA = "drycontroller.registry-auth.v2"
REGISTRY = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?$")


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleasePaths:
    lock: Path
    journal: Path
    request: Path
    active: Path
    previous: Path
    candidate: Path
    history: Path
    project: Path
    compose: Path
    cosign_pubkey: Path
    device: Path
    tpm_observation: Path
    license: Path
    license_trust_policy: Path
    cloud_config: Path
    registry_auth: Path

    @classmethod
    def production(cls) -> "ReleasePaths":
        state = Path("/var/lib/drycontroller-ota")
        return cls(
            lock=state / "release.lock",
            journal=state / "release-journal.json",
            request=state / "request.json",
            active=state / "active.json",
            previous=state / "previous.json",
            candidate=state / "candidate.json",
            history=state / "history",
            project=Path("/opt/drycontroller-host/current"),
            compose=Path("/opt/drycontroller-host/current/docker-compose.yml"),
            cosign_pubkey=Path("/etc/drycontroller/keys/cosign.pub"),
            device=Path("/var/lib/drycontroller/device.json"),
            tpm_observation=Path("/run/drycontroller-identity/tpm-observation.json"),
            license=Path("/etc/drycontroller/license/license.json"),
            license_trust_policy=Path("/etc/drycontroller/keys/license-trust/policy.json"),
            cloud_config=Path("/etc/drycontroller/cloud-ops.env"),
            registry_auth=state / "registry-auth.json",
        )


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def canonical_manifest_digest(manifest: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()


def deployment_digest(compose: Path) -> str:
    contract = {
        "compose_sha256": "sha256:" + hashlib.sha256(compose.read_bytes()).hexdigest(),
        "health": HEALTH_CONTRACT,
        "profile": EDGE_RELEASE_PROFILE,
        "schema": "drycontroller.app-deployment-contract.v1",
    }
    return "sha256:" + hashlib.sha256(canonical_json_bytes(contract)).hexdigest()


def compose_command(
    *,
    project: Path,
    compose: Path,
    override: Path,
    command: list[str],
) -> list[str]:
    return [
        COMPOSE_BINARY,
        "--project-name",
        COMPOSE_PROJECT_NAME,
        "--project-directory",
        str(project),
        "--file",
        str(compose),
        "--file",
        str(override),
        *command,
    ]


def verify_rendered_compose(
    *,
    project: Path,
    compose: Path,
    override: Path,
    images: dict[str, str],
    expected_line_id: str,
    command_runner: Callable[[list[str]], str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        rendered = json.loads(
            command_runner(
                compose_command(
                    project=project,
                    compose=compose,
                    override=override,
                    command=["config", "--format", "json"],
                )
            )
        )
        services = rendered["services"]
        networks = rendered.get("networks", {})
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ReleaseError("rendered edge-app-v1 deployment invalid") from exc
    if (
        not isinstance(rendered, dict)
        or rendered.get("name") != COMPOSE_PROJECT_NAME
        or not isinstance(services, dict)
        or not isinstance(networks, dict)
    ):
        raise ReleaseError("rendered edge-app-v1 deployment invalid")
    edge_daq = services.get("edge-daq")
    edge_daq_environment = edge_daq.get("environment") if isinstance(edge_daq, dict) else None
    if (
        not isinstance(expected_line_id, str)
        or not expected_line_id
        or not isinstance(edge_daq_environment, dict)
        or edge_daq_environment.get("LINE_ID") != expected_line_id
    ):
        raise ReleaseError("rendered edge-app-v1 LINE_ID does not match device identity")
    for service_name in EDGE_SERVICES:
        service = services.get(service_name)
        if not isinstance(service, dict) or images.get(service_name) != service.get("image"):
            raise ReleaseError("rendered edge-app-v1 image does not match release")
    return services, networks


def prove_compose_runtime(
    *,
    project: Path,
    compose: Path,
    override: Path,
    images: dict[str, str],
    expected_line_id: str,
    command_runner: Callable[[list[str]], str],
    expected_image_ids: dict[str, str] | None = None,
    require_healthy: bool = True,
) -> None:
    services, networks = verify_rendered_compose(
        project=project,
        compose=compose,
        override=override,
        images=images,
        expected_line_id=expected_line_id,
        command_runner=command_runner,
    )
    expected_config_files = f"{compose},{override}"
    for service_name in EDGE_SERVICES:
        service = services.get(service_name)
        expected_image = images.get(service_name)
        assert isinstance(service, dict) and isinstance(expected_image, str)
        raw_hash = command_runner(
            compose_command(
                project=project,
                compose=compose,
                override=override,
                command=["config", "--hash", service_name],
            )
        ).strip()
        tokens = raw_hash.split()
        if (
            len(tokens) != 2
            or tokens[0] != service_name
            or COMPOSE_CONFIG_HASH.fullmatch(tokens[1]) is None
        ):
            raise ReleaseError("rendered edge-app-v1 config hash invalid")
        config_hash = tokens[1]
        container_id = command_runner(
            compose_command(
                project=project,
                compose=compose,
                override=override,
                command=["ps", "--quiet", service_name],
            )
        ).strip()
        if CONTAINER_ID.fullmatch(container_id) is None:
            raise ReleaseError("canonical edge-app-v1 container is unavailable")
        try:
            image = json.loads(
                command_runner(["/usr/bin/docker", "image", "inspect", expected_image])
            )[0]
            image_id = image["Id"]
            container = json.loads(
                command_runner(["/usr/bin/docker", "inspect", container_id])
            )[0]
            labels = container["Config"]["Labels"]
            running_networks = set(container["NetworkSettings"]["Networks"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ReleaseError("running edge-app-v1 inspection invalid") from exc
        if (
            not isinstance(image_id, str)
            or DIGEST.fullmatch(image_id) is None
            or not isinstance(labels, dict)
        ):
            raise ReleaseError("running edge-app-v1 inspection invalid")
        expected_networks: set[str] = set()
        service_networks = service.get("networks", {})
        if not isinstance(service_networks, dict):
            raise ReleaseError("rendered edge-app-v1 networks invalid")
        for network_name in service_networks:
            network = networks.get(network_name)
            if not isinstance(network, dict) or not isinstance(network.get("name"), str):
                raise ReleaseError("rendered edge-app-v1 networks invalid")
            expected_networks.add(network["name"])
        approved_image_id = expected_image_ids.get(service_name) if expected_image_ids else image_id
        # The Compose config hash binds the fully rendered immutable container
        # configuration (including env, mounts, ports, and declared networks).
        # Network attachment itself remains mutable, so compare it explicitly.
        checks = {
            "approved-image": approved_image_id == image_id,
            "container-id": container.get("Id") == container_id,
            "image-id": container.get("Image") == image_id,
            "image-reference": container["Config"].get("Image") == expected_image,
            "running": container["State"].get("Running") is True,
            "health": (
                not require_healthy
                or container["State"].get("Health", {}).get("Status") == "healthy"
            ),
            "project": labels.get("com.docker.compose.project") == COMPOSE_PROJECT_NAME,
            "service": labels.get("com.docker.compose.service") == service_name,
            "config-hash": labels.get("com.docker.compose.config-hash") == config_hash,
            "working-dir": labels.get("com.docker.compose.project.working_dir") == str(project),
            "config-files": (
                labels.get("com.docker.compose.project.config_files") == expected_config_files
            ),
            "networks": running_networks == expected_networks,
        }
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise ReleaseError(
                "running edge-app-v1 does not match canonical Compose: " + ",".join(failed)
            )


def validate_manifest(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != MANIFEST_FIELDS:
        raise ReleaseError("manifest fields do not match the canonical v2 schema")
    if value.get("schema") != SOFTWARE_RELEASE_SCHEMA:
        raise ReleaseError("unsupported software release schema")
    if value.get("profile") != EDGE_RELEASE_PROFILE:
        raise ReleaseError("unsupported software release profile")
    try:
        release_id = str(uuid.UUID(str(value.get("release_id"))))
    except ValueError as exc:
        raise ReleaseError("manifest release_id must be a canonical UUID") from exc
    if release_id != value.get("release_id"):
        raise ReleaseError("manifest release_id must be a canonical UUID")
    sequence = value.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ReleaseError("manifest sequence must be a positive integer")
    source_commit = value.get("source_commit")
    if not isinstance(source_commit, str) or COMMIT.fullmatch(source_commit) is None:
        raise ReleaseError("manifest source_commit must be lowercase 40-hex")
    digest = value.get("deployment_digest")
    if not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
        raise ReleaseError("manifest deployment_digest must be a sha256 digest")
    images = value.get("images")
    if not isinstance(images, dict) or set(images) != set(EDGE_SERVICES):
        raise ReleaseError("edge-app-v1 images must be exactly edge-api and edge-daq")
    for service in EDGE_SERVICES:
        reference = images.get(service)
        if not isinstance(reference, str) or IMAGE_REFERENCE.fullmatch(reference) is None:
            raise ReleaseError(f"service {service} image must be an immutable digest reference")
    return value


def validate_slot(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != SLOT_FIELDS:
        raise ReleaseError("release slot fields are invalid")
    manifest = validate_manifest(value.get("manifest"))
    if value.get("manifest_digest") != canonical_manifest_digest(manifest):
        raise ReleaseError("release slot manifest digest mismatch")
    if not isinstance(value.get("manifest_signature"), str) or not value["manifest_signature"]:
        raise ReleaseError("release slot manifest signature missing")
    verified_at = value.get("verified_at")
    if not isinstance(verified_at, str) or not verified_at:
        raise ReleaseError("release slot verification time missing")
    return value


def _validate_release_id(value: object, label: str) -> str:
    try:
        canonical = str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise ReleaseError(f"{label} must be a canonical UUID") from exc
    if canonical != value:
        raise ReleaseError(f"{label} must be a canonical UUID")
    return canonical


def validate_release_result(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseError("release result invalid")
    action = value.get("action")
    if action not in {"apply", "rollback"}:
        raise ReleaseError("release result action invalid")
    expected_fields = APPLY_RESULT_FIELDS if action == "apply" else ROLLBACK_RESULT_FIELDS
    if set(value) != expected_fields:
        raise ReleaseError("release result fields invalid")
    operation_id = value.get("operation_id")
    if not isinstance(operation_id, str) or OPERATION_ID.fullmatch(operation_id) is None:
        raise ReleaseError("release result operation invalid")
    expected_status = "committed" if action == "apply" else "rolled_back"
    if value.get("status") != expected_status:
        raise ReleaseError("release result status invalid")
    _validate_release_id(value.get("release_id"), "release result release_id")
    _validate_release_id(value.get("previous_release_id"), "release result previous_release_id")
    if action == "rollback":
        _validate_release_id(
            value.get("expected_active_release_id"),
            "rollback expected_active_release_id",
        )
        _validate_release_id(value.get("target_release_id"), "rollback target_release_id")
        if (
            value["release_id"] != value["target_release_id"]
            or value["previous_release_id"] != value["expected_active_release_id"]
        ):
            raise ReleaseError("rollback result binding invalid")
    digest = value.get("manifest_digest")
    if not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
        raise ReleaseError("release result manifest digest invalid")
    sequence = value.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ReleaseError("release result sequence invalid")
    return value


def validate_release_journal(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != JOURNAL_FIELDS:
        raise ReleaseError("release journal fields invalid")
    if value.get("schema") != RELEASE_JOURNAL_SCHEMA:
        raise ReleaseError("release journal schema invalid")
    if value.get("phase") not in {"idle", "prepared", "mutated", "committed"}:
        raise ReleaseError("release journal phase invalid")
    operation_id = value.get("operation_id")
    if operation_id is not None and (
        not isinstance(operation_id, str) or OPERATION_ID.fullmatch(operation_id) is None
    ):
        raise ReleaseError("release journal operation invalid")
    for name in ("active", "previous", "candidate"):
        if value.get(name) is not None:
            validate_slot(value[name])
    if value.get("active") is None:
        raise ReleaseError("release journal active slot missing")
    if value["phase"] == "idle" and operation_id is not None:
        raise ReleaseError("idle release journal operation must be empty")
    if value["phase"] in {"prepared", "mutated", "committed"} and operation_id is None:
        raise ReleaseError("release journal operation missing")
    high_watermark = value.get("high_watermark")
    if isinstance(high_watermark, bool) or not isinstance(high_watermark, int) or high_watermark < 1:
        raise ReleaseError("release journal high watermark invalid")
    slot_sequences = [
        value[name]["manifest"]["sequence"]
        for name in ("active", "previous")
        if value.get(name) is not None
    ]
    if high_watermark < max(slot_sequences):
        raise ReleaseError("release journal high watermark regressed")
    last_operation_id = value.get("last_operation_id")
    last_result = value.get("last_result")
    if last_operation_id is None:
        if last_result is not None:
            raise ReleaseError("release journal last result missing operation")
    else:
        if not isinstance(last_operation_id, str) or OPERATION_ID.fullmatch(last_operation_id) is None:
            raise ReleaseError("release journal last operation invalid")
        result = validate_release_result(last_result)
        if result["operation_id"] != last_operation_id:
            raise ReleaseError("release journal last operation binding mismatch")
        active = value["active"]
        if (
            result["release_id"] != active["manifest"]["release_id"]
            or result["manifest_digest"] != active["manifest_digest"]
            or result["sequence"] != active["manifest"]["sequence"]
            or result["sequence"] > high_watermark
        ):
            raise ReleaseError("release journal last result does not match active release")
    if value["phase"] == "committed" and operation_id != last_operation_id:
        raise ReleaseError("committed release journal result missing")
    if value.get("error") is not None and not isinstance(value["error"], str):
        raise ReleaseError("release journal error invalid")
    return value


def verify_manifest_signature(manifest: dict[str, Any], encoded_bundle: str, public_key_path: Path) -> None:
    try:
        public_key = serialization.load_pem_public_key(public_key_path.read_bytes())
        bundle = json.loads(base64.b64decode(encoded_bundle, validate=True))
    except (OSError, ValueError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("release manifest signature unavailable") from exc
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        raise ReleaseError("release manifest public key invalid")
    try:
        if set(bundle) != {"mediaType", "verificationMaterial", "messageSignature"}:
            raise KeyError
        if bundle["mediaType"] != "application/vnd.dev.sigstore.bundle.v0.3+json":
            raise KeyError
        material = bundle["verificationMaterial"]
        message = bundle["messageSignature"]
        digest_record = message["messageDigest"]
        if set(material) != {"publicKey"} or set(material["publicKey"]) != {"hint"}:
            raise KeyError
        if set(message) != {"messageDigest", "signature"} or set(digest_record) != {
            "algorithm",
            "digest",
        }:
            raise KeyError
        if digest_record["algorithm"] != "SHA2_256":
            raise KeyError
        digest = base64.b64decode(digest_record["digest"], validate=True)
        signature = base64.b64decode(message["signature"], validate=True)
        hint = base64.b64decode(material["publicKey"]["hint"], validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise ReleaseError("release manifest signature bundle invalid") from exc
    expected_digest = hashlib.sha256(canonical_json_bytes(manifest)).digest()
    public_der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if digest != expected_digest or hint != hashlib.sha256(public_der).digest():
        raise ReleaseError("release manifest signature binding mismatch")
    try:
        public_key.verify(signature, digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    except (InvalidSignature, ValueError) as exc:
        raise ReleaseError("release manifest signature invalid") from exc


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json_bytes(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if not raw or len(raw) > MAX_JSON_BYTES:
            raise ValueError
        value = json.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"invalid release JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise ReleaseError(f"invalid release JSON: {path.name}")
    return value


def read_device_line_id(path: Path) -> str:
    line_id = read_json(path).get("line_id")
    if not isinstance(line_id, str) or not line_id or line_id.strip() != line_id:
        raise ReleaseError("device identity line_id invalid")
    return line_id


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def read_registry_auth(path: Path, reference: str) -> dict[str, Any]:
    descriptor = -1
    try:
        metadata = path.lstat()
        parent = path.parent.lstat()
        if (
            path.resolve(strict=True) != path
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or metadata.st_nlink != 1
            or path.parent.resolve(strict=True) != path.parent
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise OSError("registry auth metadata invalid")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        opened = os.fstat(descriptor)
        raw = os.read(descriptor, MAX_JSON_BYTES + 1)
        if (
            (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or not raw
            or len(raw) > MAX_JSON_BYTES
        ):
            raise OSError("registry auth changed")
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
        or not isinstance(payload.get("auths"), dict)
        or len(payload["auths"]) != 1
    ):
        raise ReleaseError("root registry auth source invalid")
    registry = reference.split("/", 1)[0]
    if REGISTRY.fullmatch(registry) is None or set(payload["auths"]) != {registry}:
        raise ReleaseError("release image registry does not match active credential")
    entry = payload["auths"].get(registry)
    credential = payload.get("credential")
    if (
        not isinstance(entry, dict)
        or set(entry) != {"auth"}
        or not isinstance(entry.get("auth"), str)
        or not isinstance(credential, dict)
        or set(credential)
        != {
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
        or credential.get("registry_host") != registry
        or not isinstance(credential.get("username"), str)
        or not credential["username"]
        or not isinstance(credential.get("secret_fingerprint"), str)
        or not credential["secret_fingerprint"]
    ):
        raise ReleaseError("root registry auth source invalid")
    try:
        decoded = base64.b64decode(entry["auth"], validate=True)
        username, separator, secret = decoded.partition(b":")
        bound_username = username.decode("utf-8")
    except (UnicodeDecodeError, binascii.Error, ValueError) as exc:
        raise ReleaseError("root registry auth source invalid") from exc
    if (
        not username
        or separator != b":"
        or not secret
        or len(decoded) > 4096
        or bound_username != credential["username"]
        or not hmac.compare_digest(
            hashlib.sha256(secret).hexdigest()[:16],
            credential["secret_fingerprint"],
        )
    ):
        raise ReleaseError("root registry auth source invalid")
    return {"auths": {registry: {"auth": entry["auth"]}}}


class ReleaseEngine:
    def __init__(
        self,
        *,
        paths: ReleasePaths | None = None,
        command_runner: Callable[[list[str]], str] | None = None,
        policy_fetcher: Callable[[str], dict[str, Any]] | None = None,
        health_checker: Callable[[str], None] | None = None,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.paths = paths or ReleasePaths.production()
        self.command_runner = command_runner or self._run
        self.policy_fetcher = policy_fetcher or self._fetch_policy
        self.health_checker = health_checker or self._health
        self.fault = fault or (lambda _point: None)

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        self.paths.lock.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.paths.lock,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            if os.fstat(descriptor).st_nlink != 1:
                raise ReleaseError("release transaction lock invalid")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ReleaseError("another release transaction is running") from exc
            yield
        finally:
            os.close(descriptor)

    def _run(self, args: list[str]) -> str:
        registry_reference = None
        if args[:2] == ["/usr/bin/docker", "pull"] or args[:2] == [
            "/usr/local/bin/cosign",
            "verify",
        ]:
            registry_reference = args[-1]
        environment = (
            self._registry_environment(registry_reference)
            if registry_reference is not None
            else contextlib.nullcontext(FIXED_ENV)
        )
        try:
            with environment as command_environment:
                completed = subprocess.run(
                    args,
                    cwd=self.paths.project,
                    env=command_environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                    timeout=300,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseError(f"command unavailable: {Path(args[0]).name}") from exc
        if completed.returncode != 0:
            raise ReleaseError(f"command failed: {Path(args[0]).name}")
        return completed.stdout

    @contextlib.contextmanager
    def _registry_environment(self, reference: str) -> Iterator[dict[str, str]]:
        payload = read_registry_auth(self.paths.registry_auth, reference)
        try:
            with tempfile.TemporaryDirectory(
                prefix=".edge-app-docker-config-",
                dir=self.paths.registry_auth.parent,
            ) as temporary:
                directory = Path(temporary)
                directory.chmod(0o700)
                config = directory / "config.json"
                descriptor = os.open(
                    config,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                )
                try:
                    os.write(descriptor, canonical_json_bytes(payload))
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                yield {**FIXED_ENV, "DOCKER_CONFIG": str(directory)}
        except OSError as exc:
            raise ReleaseError("private Docker auth config unavailable") from exc

    @staticmethod
    def _health(url: str) -> None:
        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
        last_error: Exception | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = (
                    f"{type(last_error).__name__}: {last_error}"
                    if last_error is not None
                    else "no response"
                )
                raise ReleaseError(f"release health check failed; last error: {detail}") from last_error
            try:
                with urllib.request.urlopen(
                    url,
                    timeout=min(HEALTH_ATTEMPT_TIMEOUT_SECONDS, remaining),
                ) as response:
                    if response.status == 200:
                        return
                    last_error = ReleaseError(f"unexpected HTTP status {response.status}")
            except (OSError, urllib.error.URLError) as exc:
                last_error = exc
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(HEALTH_POLL_SECONDS, remaining))

    def _cloud_credentials(self) -> tuple[str, str]:
        values: dict[str, str] = {}
        try:
            for raw in self.paths.cloud_config.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key == "CLOUD_OPS_BASE_URL":
                    tokens = shlex.split(value)
                    if len(tokens) != 1 or key in values:
                        raise ValueError
                    values[key] = tokens[0]
            identity = read_json(self.paths.device)
            credential = identity["runtime_credential"]
            credential_hash = identity["runtime_credential_hash"]
        except (OSError, UnicodeDecodeError, ValueError, KeyError) as exc:
            raise ReleaseError("cloud policy configuration unavailable") from exc
        base = values.get("CLOUD_OPS_BASE_URL", "")
        if not base.startswith("https://") or not isinstance(credential, str) or not hmac.compare_digest(
            hashlib.sha256(credential.encode()).hexdigest(), str(credential_hash)
        ):
            raise ReleaseError("cloud policy configuration unavailable")
        return base.rstrip("/"), credential

    def _fetch_policy(self, operation_id: str) -> dict[str, Any]:
        base, credential = self._cloud_credentials()
        request = urllib.request.Request(
            f"{base}/api/v1/ota/release-policy/{operation_id}",
            headers={"X-Device-Credential": credential, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES:
                raise ValueError
            value = json.loads(raw)
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("release policy fetch failed") from exc
        if not isinstance(value, dict):
            raise ReleaseError("release policy response invalid")
        return value

    def _verify_deployment(self, manifest: dict[str, Any]) -> None:
        try:
            observed = deployment_digest(self.paths.compose)
        except OSError as exc:
            raise ReleaseError("installed deployment contract unavailable") from exc
        if manifest["deployment_digest"] != observed:
            raise ReleaseError("release deployment digest mismatch")

    def _verify_image_signatures(self, manifest: dict[str, Any]) -> None:
        for service in EDGE_SERVICES:
            self.command_runner(
                [
                    "/usr/local/bin/cosign",
                    "verify",
                    "--insecure-ignore-tlog=true",
                    "--key",
                    str(self.paths.cosign_pubkey),
                    manifest["images"][service],
                ]
            )

    def _pull(self, manifest: dict[str, Any]) -> None:
        for service in EDGE_SERVICES:
            self.command_runner(["/usr/bin/docker", "pull", manifest["images"][service]])

    def _prove_local(self, slot: dict[str, Any]) -> None:
        manifest = slot["manifest"]
        for service in EDGE_SERVICES:
            self.command_runner(["/usr/bin/docker", "image", "inspect", manifest["images"][service]])

    def _override(self, slot: dict[str, Any]) -> Path:
        target = self.paths.journal.parent / "edge-app-images.yml"
        images = slot["manifest"]["images"]
        content = (
            "services:\n"
            f"  edge-api:\n    image: {json.dumps(images['edge-api'])}\n"
            f"  edge-daq:\n    image: {json.dumps(images['edge-daq'])}\n"
        ).encode()
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def _verify_rendered(self, slot: dict[str, Any]) -> Path:
        override = self._override(slot)
        verify_rendered_compose(
            project=self.paths.project,
            compose=self.paths.compose,
            override=override,
            images=slot["manifest"]["images"],
            expected_line_id=read_device_line_id(self.paths.device),
            command_runner=self.command_runner,
        )
        return override

    def _compose(self, slot: dict[str, Any]) -> None:
        override = self._verify_rendered(slot)
        self.command_runner(
            compose_command(
                project=self.paths.project,
                compose=self.paths.compose,
                override=override,
                command=[
                    "up",
                    "-d",
                    "--no-deps",
                    "--pull",
                    "never",
                    *EDGE_SERVICES,
                ],
            )
        )

    def _verify_runtime(self) -> None:
        for service in EDGE_SERVICES:
            self.health_checker(HEALTH_CONTRACT[service])

    def _verify_license(self) -> None:
        result = verify_installed_license(
            license_path=self.paths.license,
            trust_policy_path=self.paths.license_trust_policy,
            device_identity_path=self.paths.device,
            tpm_observation_path=self.paths.tpm_observation,
            required_capability="edge-app-ota",
        )
        if result.get("valid") is not True:
            raise ReleaseError(f"License rejected: {result.get('reason', 'invalid')}")

    def _verify_slot(self, slot: dict[str, Any], *, images: bool) -> None:
        manifest = validate_manifest(slot["manifest"])
        self._verify_deployment(manifest)
        verify_manifest_signature(manifest, slot["manifest_signature"], self.paths.cosign_pubkey)
        if images:
            self._verify_image_signatures(manifest)

    @staticmethod
    def _slot(manifest: dict[str, Any], signature: str) -> dict[str, Any]:
        return {
            "manifest": manifest,
            "manifest_digest": canonical_manifest_digest(manifest),
            "manifest_signature": signature,
            "verified_at": dt.datetime.now(dt.UTC).isoformat(),
        }

    def _journal(
        self,
        *,
        phase: str,
        operation_id: str | None,
        last_operation_id: str | None,
        last_result: dict[str, Any] | None,
        high_watermark: int,
        active: dict[str, Any],
        previous: dict[str, Any] | None,
        candidate: dict[str, Any] | None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "active": active,
            "candidate": candidate,
            "error": error,
            "high_watermark": high_watermark,
            "last_operation_id": last_operation_id,
            "last_result": last_result,
            "operation_id": operation_id,
            "phase": phase,
            "previous": previous,
            "schema": RELEASE_JOURNAL_SCHEMA,
        }

    def _persist(self, journal: dict[str, Any]) -> None:
        validate_release_journal(journal)
        atomic_write(self.paths.journal, journal)

    def _load_journal(self) -> dict[str, Any]:
        return validate_release_journal(read_json(self.paths.journal))

    @staticmethod
    def _journal_metadata(journal: dict[str, Any]) -> dict[str, Any]:
        return {
            "last_operation_id": journal["last_operation_id"],
            "last_result": journal["last_result"],
            "high_watermark": journal["high_watermark"],
        }

    def _load_active(self) -> dict[str, Any]:
        return validate_slot(read_json(self.paths.active))

    def initialize_active(self, seed: object) -> dict[str, Any]:
        with self._lock():
            if not isinstance(seed, dict) or set(seed) != {
                "manifest",
                "manifest_digest",
                "manifest_signature",
            }:
                raise ReleaseError("initial release seed fields invalid")
            manifest = validate_manifest(seed["manifest"])
            slot = self._slot(manifest, str(seed["manifest_signature"]))
            if seed["manifest_digest"] != slot["manifest_digest"]:
                raise ReleaseError("initial release manifest digest mismatch")
            if self.paths.previous.exists():
                raise ReleaseError("edge-app-v1 baseline previous release must be absent")
            if self.paths.active.exists():
                existing = self._load_active()
                if any(
                    existing[name] != slot[name]
                    for name in ("manifest", "manifest_digest", "manifest_signature")
                ):
                    raise ReleaseError("existing edge-app-v1 active baseline does not match seed")
                self._verify_slot(existing, images=True)
                self._prove_running_matches(existing)
                if self.paths.journal.exists():
                    journal = self._load_journal()
                    if (
                        journal["phase"] != "idle"
                        or journal["active"] != existing
                        or journal["previous"] is not None
                        or journal["candidate"] is not None
                        or journal["last_operation_id"] is not None
                        or journal["last_result"] is not None
                        or journal["high_watermark"] != manifest["sequence"]
                    ):
                        raise ReleaseError("existing edge-app-v1 baseline journal is not initial state")
                else:
                    self._persist(
                        self._journal(
                            phase="idle",
                            operation_id=None,
                            last_operation_id=None,
                            last_result=None,
                            high_watermark=manifest["sequence"],
                            active=existing,
                            previous=None,
                            candidate=None,
                        )
                    )
                return {
                    "manifest_digest": existing["manifest_digest"],
                    "previous": None,
                    "release_id": manifest["release_id"],
                    "sequence": manifest["sequence"],
                    "status": "already_active",
                }
            if self.paths.journal.exists():
                raise ReleaseError("edge-app-v1 baseline journal exists without active release")
            self._verify_slot(slot, images=True)
            self._prove_running_matches(slot)
            atomic_write(self.paths.active, slot)
            self.fault("initialize_after_active_write")
            self._persist(
                self._journal(
                    phase="idle",
                    operation_id=None,
                    last_operation_id=None,
                    last_result=None,
                    high_watermark=manifest["sequence"],
                    active=slot,
                    previous=None,
                    candidate=None,
                )
            )
            return {
                "manifest_digest": slot["manifest_digest"],
                "previous": None,
                "release_id": manifest["release_id"],
                "sequence": manifest["sequence"],
                "status": "active",
            }

    def _prove_running_matches(
        self,
        slot: dict[str, Any],
        *,
        require_healthy: bool = True,
    ) -> None:
        override = self._override(slot)
        prove_compose_runtime(
            project=self.paths.project,
            compose=self.paths.compose,
            override=override,
            images=slot["manifest"]["images"],
            expected_line_id=read_device_line_id(self.paths.device),
            command_runner=self.command_runner,
            require_healthy=require_healthy,
        )

    def _validate_policy(self, value: object, operation_id: str, active: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != POLICY_FIELDS:
            raise ReleaseError("release policy fields invalid")
        if value.get("operation_id") != operation_id:
            raise ReleaseError("release policy operation binding mismatch")
        node_id = value.get("node_id")
        if isinstance(node_id, bool) or not isinstance(node_id, int) or node_id != int(operation_id[16:], 16):
            raise ReleaseError("release policy node binding mismatch")
        identity = read_json(self.paths.device)
        for policy_field, identity_field in (
            ("tenant_id", "tenant_id"),
            ("line_id", "line_id"),
            ("device_id", "device_id"),
            ("node_name", "node_name"),
        ):
            if value.get(policy_field) != identity.get(identity_field):
                raise ReleaseError("release policy device binding mismatch")
        manifest = validate_manifest(value.get("manifest"))
        digest = canonical_manifest_digest(manifest)
        signature = value.get("manifest_signature")
        if (
            value.get("release_id") != manifest["release_id"]
            or value.get("manifest_digest") != digest
            or not isinstance(signature, str)
            or not signature
        ):
            raise ReleaseError("release policy manifest binding mismatch")
        return self._slot(manifest, signature)

    def _read_operation_id(self) -> str:
        request = read_json(self.paths.request)
        if set(request) != {"operation_id"}:
            raise ReleaseError("release request must contain only operation_id")
        operation_id = request.get("operation_id")
        if not isinstance(operation_id, str) or OPERATION_ID.fullmatch(operation_id) is None:
            raise ReleaseError("release request operation_id invalid")
        return operation_id

    def _restore(
        self,
        active: dict[str, Any],
        previous: dict[str, Any] | None,
        operation_id: str | None,
        metadata: dict[str, Any],
    ) -> None:
        self._verify_rendered(active)
        self._prove_local(active)
        self._compose(active)
        self._verify_runtime()
        self._prove_running_matches(active, require_healthy=False)
        atomic_write(self.paths.active, active)
        if previous is None:
            self.paths.previous.unlink(missing_ok=True)
        else:
            atomic_write(self.paths.previous, previous)
        self.paths.candidate.unlink(missing_ok=True)
        self._persist(
            self._journal(
                phase="idle",
                operation_id=None,
                **metadata,
                active=active,
                previous=previous,
                candidate=None,
                error=f"operation {operation_id} rolled back" if operation_id else None,
            )
        )

    def _recover_locked(self) -> None:
        journal = self._load_journal()
        if journal["phase"] == "idle":
            return
        if journal["phase"] == "committed":
            self._finish_committed(journal)
            return
        active = validate_slot(journal["active"])
        previous = validate_slot(journal["previous"]) if journal["previous"] else None
        if journal["phase"] == "mutated":
            self._restore(
                active,
                previous,
                journal["operation_id"],
                self._journal_metadata(journal),
            )
            return
        self.paths.candidate.unlink(missing_ok=True)
        self._persist(
            self._journal(
                phase="idle",
                operation_id=None,
                **self._journal_metadata(journal),
                active=active,
                previous=previous,
                candidate=None,
                error=f"operation {journal['operation_id']} abandoned before mutation",
            )
        )

    def _finish_committed(self, journal: dict[str, Any]) -> None:
        journal = validate_release_journal(journal)
        if journal["phase"] != "committed":
            raise ReleaseError("release journal is not committed")
        active = validate_slot(journal["active"])
        previous = validate_slot(journal["previous"]) if journal["previous"] else None
        atomic_write(self.paths.active, active)
        if previous is None:
            self.paths.previous.unlink(missing_ok=True)
        else:
            atomic_write(self.paths.previous, previous)
        self.paths.candidate.unlink(missing_ok=True)
        self.paths.history.mkdir(parents=True, exist_ok=True)
        atomic_write(self.paths.history / f"{journal['operation_id']}.json", journal)
        idle = dict(journal)
        idle.update({"phase": "idle", "operation_id": None, "candidate": None, "error": None})
        self._persist(idle)

    @staticmethod
    def _apply_replay(journal: dict[str, Any], operation_id: str) -> dict[str, Any] | None:
        if journal["last_operation_id"] != operation_id:
            return None
        result = validate_release_result(journal["last_result"])
        active = journal["active"]
        if (
            result["action"] != "apply"
            or result["release_id"] != active["manifest"]["release_id"]
            or result["manifest_digest"] != active["manifest_digest"]
            or result["sequence"] != active["manifest"]["sequence"]
        ):
            raise ReleaseError("release operation replay does not match committed release")
        return dict(result)

    @staticmethod
    def _rollback_replay(
        journal: dict[str, Any],
        operation_id: str,
        expected_active_release_id: str,
        target_release_id: str,
    ) -> dict[str, Any] | None:
        if journal["last_operation_id"] != operation_id:
            return None
        result = validate_release_result(journal["last_result"])
        previous = journal["previous"]
        if (
            result["action"] != "rollback"
            or result["expected_active_release_id"] != expected_active_release_id
            or result["target_release_id"] != target_release_id
            or journal["active"]["manifest"]["release_id"] != target_release_id
            or previous is None
            or previous["manifest"]["release_id"] != expected_active_release_id
        ):
            raise ReleaseError("rollback operation replay binding mismatch")
        return dict(result)

    def recover(self) -> dict[str, Any]:
        with self._lock():
            self._recover_locked()
            active = self._load_active()
            return {
                "status": "recovered",
                "release_id": active["manifest"]["release_id"],
                "manifest_digest": active["manifest_digest"],
                "sequence": active["manifest"]["sequence"],
            }

    def apply(self) -> dict[str, Any]:
        with self._lock():
            self._recover_locked()
            state = self._load_journal()
            active = self._load_active()
            if active != state["active"]:
                raise ReleaseError("active release file does not match journal")
            previous = state["previous"]
            operation_id = self._read_operation_id()
            replay = self._apply_replay(state, operation_id)
            if replay is not None:
                self._verify_slot(active, images=False)
                self._prove_local(active)
                return replay
            candidate = self._validate_policy(self.policy_fetcher(operation_id), operation_id, active)
            if candidate["manifest"]["sequence"] <= state["high_watermark"]:
                raise ReleaseError("release sequence must increase beyond committed high watermark")

            self._verify_license()
            self._verify_slot(candidate, images=True)
            self._verify_rendered(candidate)
            self._prove_local(active)
            self._pull(candidate["manifest"])
            atomic_write(self.paths.candidate, candidate)
            journal = self._journal(
                phase="prepared",
                operation_id=operation_id,
                **self._journal_metadata(state),
                active=active,
                previous=previous,
                candidate=candidate,
            )
            self._persist(journal)
            self.fault("after_prepared")
            try:
                journal["phase"] = "mutated"
                self._persist(journal)
                self._compose(candidate)
                self.fault("after_mutated")
                self._verify_runtime()
                self._prove_running_matches(candidate, require_healthy=False)
                self.fault("after_health")
                atomic_write(self.paths.previous, active)
                atomic_write(self.paths.active, candidate)
                result = {
                    "action": "apply",
                    "manifest_digest": candidate["manifest_digest"],
                    "operation_id": operation_id,
                    "previous_release_id": active["manifest"]["release_id"],
                    "release_id": candidate["manifest"]["release_id"],
                    "sequence": candidate["manifest"]["sequence"],
                    "status": "committed",
                }
                journal = self._journal(
                    phase="committed",
                    operation_id=operation_id,
                    last_operation_id=operation_id,
                    last_result=result,
                    high_watermark=candidate["manifest"]["sequence"],
                    active=candidate,
                    previous=active,
                    candidate=None,
                )
                self._persist(journal)
            except Exception:
                self._restore(active, previous, operation_id, self._journal_metadata(state))
                raise
            self.fault("after_committed")
            self._finish_committed(journal)
            return result

    def start_active(self) -> dict[str, Any]:
        with self._lock():
            self._recover_locked()
            active = self._load_active()
            self._verify_slot(active, images=False)
            self._verify_rendered(active)
            self._prove_local(active)
            self._compose(active)
            self._verify_runtime()
            self._prove_running_matches(active, require_healthy=False)
            return {
                "release_id": active["manifest"]["release_id"],
                "manifest_digest": active["manifest_digest"],
                "sequence": active["manifest"]["sequence"],
                "status": "active",
            }

    def rollback(
        self,
        *,
        operation_id: str,
        expected_active_release_id: str,
        target_release_id: str,
    ) -> dict[str, Any]:
        with self._lock():
            if not isinstance(operation_id, str) or OPERATION_ID.fullmatch(operation_id) is None:
                raise ReleaseError("rollback operation_id invalid")
            _validate_release_id(expected_active_release_id, "rollback expected active release")
            _validate_release_id(target_release_id, "rollback target release")
            self._recover_locked()
            state = self._load_journal()
            replay = self._rollback_replay(
                state,
                operation_id,
                expected_active_release_id,
                target_release_id,
            )
            if replay is not None:
                return replay
            active = self._load_active()
            if active != state["active"]:
                raise ReleaseError("active release file does not match journal")
            if not self.paths.previous.exists():
                raise ReleaseError("verified previous release is unavailable")
            previous = validate_slot(read_json(self.paths.previous))
            if active["manifest"]["release_id"] != expected_active_release_id:
                raise ReleaseError("rollback expected active release does not match")
            if previous["manifest"]["release_id"] != target_release_id:
                raise ReleaseError("rollback target previous release does not match")
            self._verify_slot(previous, images=False)
            self._verify_rendered(previous)
            self._prove_local(previous)
            journal = self._journal(
                phase="prepared",
                operation_id=operation_id,
                **self._journal_metadata(state),
                active=active,
                previous=previous,
                candidate=previous,
            )
            self._persist(journal)
            self.fault("rollback_after_prepared")
            journal["phase"] = "mutated"
            self._persist(journal)
            self.fault("rollback_after_mutated")
            try:
                self._compose(previous)
                self._verify_runtime()
                self._prove_running_matches(previous, require_healthy=False)
                self.fault("rollback_after_runtime")
                atomic_write(self.paths.active, previous)
                self.fault("rollback_after_active_write")
                atomic_write(self.paths.previous, active)
                result = {
                    "action": "rollback",
                    "expected_active_release_id": expected_active_release_id,
                    "manifest_digest": previous["manifest_digest"],
                    "operation_id": operation_id,
                    "previous_release_id": active["manifest"]["release_id"],
                    "release_id": previous["manifest"]["release_id"],
                    "sequence": previous["manifest"]["sequence"],
                    "status": "rolled_back",
                    "target_release_id": target_release_id,
                }
                journal = self._journal(
                    phase="committed",
                    operation_id=operation_id,
                    last_operation_id=operation_id,
                    last_result=result,
                    high_watermark=state["high_watermark"],
                    active=previous,
                    previous=active,
                    candidate=None,
                )
                self._persist(journal)
            except Exception:
                self._restore(active, previous, operation_id, self._journal_metadata(state))
                raise
            self.fault("rollback_after_committed")
            self._finish_committed(journal)
            return result
