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
import subprocess
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
    "active",
    "previous",
    "candidate",
    "error",
}
OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_REFERENCE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
MAX_JSON_BYTES = 1024 * 1024
FIXED_ENV = {
    "HOME": "/root",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "DOCKER_HOST": "unix:///run/docker.sock",
    "COMPOSE_PROJECT_NAME": "drycontroller-edge",
}


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
        try:
            completed = subprocess.run(
                args,
                cwd=self.paths.project,
                env=FIXED_ENV,
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

    @staticmethod
    def _health(url: str) -> None:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                if response.status != 200:
                    raise ReleaseError("release health check failed")
        except (OSError, urllib.error.URLError) as exc:
            raise ReleaseError("release health check failed") from exc

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

    def _compose(self, slot: dict[str, Any]) -> None:
        override = self._override(slot)
        self.command_runner(
            [
                "/usr/libexec/docker/cli-plugins/docker-compose",
                "--file",
                str(self.paths.compose),
                "--file",
                str(override),
                "up",
                "-d",
                "--no-deps",
                "--pull",
                "never",
                *EDGE_SERVICES,
            ]
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
        active: dict[str, Any],
        previous: dict[str, Any] | None,
        candidate: dict[str, Any] | None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "active": active,
            "candidate": candidate,
            "error": error,
            "operation_id": operation_id,
            "phase": phase,
            "previous": previous,
            "schema": RELEASE_JOURNAL_SCHEMA,
        }

    def _persist(self, journal: dict[str, Any]) -> None:
        validate_release_journal(journal)
        atomic_write(self.paths.journal, journal)

    def _load_active(self) -> dict[str, Any]:
        return validate_slot(read_json(self.paths.active))

    def initialize_active(self, seed: object) -> dict[str, Any]:
        with self._lock():
            if self.paths.active.exists() or self.paths.previous.exists():
                raise ReleaseError("edge-app-v1 active baseline already exists")
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
            self._verify_slot(slot, images=True)
            self._prove_running_matches(slot)
            atomic_write(self.paths.active, slot)
            self._persist(
                self._journal(
                    phase="idle",
                    operation_id=None,
                    active=slot,
                    previous=None,
                    candidate=None,
                )
            )
            return {"release_id": manifest["release_id"], "previous": None, "status": "active"}

    def _prove_running_matches(self, slot: dict[str, Any]) -> None:
        for service in EDGE_SERVICES:
            raw = self.command_runner(["/usr/bin/docker", "inspect", service])
            try:
                inspected = json.loads(raw)
                container = inspected[0]
                image = container["Config"]["Image"]
                health = container["State"]["Health"]["Status"]
            except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ReleaseError("running edge-app-v1 inspection invalid") from exc
            if image != slot["manifest"]["images"][service] or health != "healthy":
                raise ReleaseError("running edge-app-v1 does not match baseline")

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
        if manifest["sequence"] <= active["manifest"]["sequence"]:
            raise ReleaseError("release sequence must increase")
        return self._slot(manifest, signature)

    def _read_operation_id(self) -> str:
        request = read_json(self.paths.request)
        if set(request) != {"operation_id"}:
            raise ReleaseError("release request must contain only operation_id")
        operation_id = request.get("operation_id")
        if not isinstance(operation_id, str) or OPERATION_ID.fullmatch(operation_id) is None:
            raise ReleaseError("release request operation_id invalid")
        return operation_id

    def _restore(self, active: dict[str, Any], previous: dict[str, Any] | None, operation_id: str | None) -> None:
        self._prove_local(active)
        self._compose(active)
        self._verify_runtime()
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
                active=active,
                previous=previous,
                candidate=None,
                error=f"operation {operation_id} rolled back" if operation_id else None,
            )
        )

    def _recover_locked(self) -> None:
        journal = validate_release_journal(read_json(self.paths.journal))
        if journal["phase"] == "idle":
            return
        if journal["phase"] == "committed":
            active = validate_slot(journal["active"])
            previous = validate_slot(journal["previous"]) if journal["previous"] else None
            atomic_write(self.paths.active, active)
            if previous:
                atomic_write(self.paths.previous, previous)
            self.paths.candidate.unlink(missing_ok=True)
            self._persist(
                self._journal(
                    phase="idle",
                    operation_id=None,
                    active=active,
                    previous=previous,
                    candidate=None,
                )
            )
            return
        active = validate_slot(journal["active"])
        previous = validate_slot(journal["previous"]) if journal["previous"] else None
        if journal["phase"] == "mutated":
            self._restore(active, previous, journal["operation_id"])
            return
        self.paths.candidate.unlink(missing_ok=True)
        self._persist(
            self._journal(
                phase="idle",
                operation_id=None,
                active=active,
                previous=previous,
                candidate=None,
                error=f"operation {journal['operation_id']} abandoned before mutation",
            )
        )

    def recover(self) -> dict[str, Any]:
        with self._lock():
            self._recover_locked()
            return {"status": "recovered", "release_id": self._load_active()["manifest"]["release_id"]}

    def apply(self) -> dict[str, Any]:
        with self._lock():
            self._recover_locked()
            active = self._load_active()
            previous = validate_slot(read_json(self.paths.previous)) if self.paths.previous.exists() else None
            operation_id = self._read_operation_id()
            candidate = self._validate_policy(self.policy_fetcher(operation_id), operation_id, active)

            self._verify_license()
            self._verify_slot(candidate, images=True)
            self._prove_local(active)
            self._pull(candidate["manifest"])
            atomic_write(self.paths.candidate, candidate)
            journal = self._journal(
                phase="prepared",
                operation_id=operation_id,
                active=active,
                previous=previous,
                candidate=candidate,
            )
            self._persist(journal)
            self.fault("after_prepared")
            mutated = True
            try:
                journal["phase"] = "mutated"
                self._persist(journal)
                self._compose(candidate)
                self.fault("after_mutated")
                self._verify_runtime()
                self.fault("after_health")
                atomic_write(self.paths.previous, active)
                atomic_write(self.paths.active, candidate)
                journal = self._journal(
                    phase="committed",
                    operation_id=operation_id,
                    active=candidate,
                    previous=active,
                    candidate=None,
                )
                self._persist(journal)
                self.fault("after_committed")
                self.paths.candidate.unlink(missing_ok=True)
                self.paths.history.mkdir(parents=True, exist_ok=True)
                atomic_write(self.paths.history / f"{operation_id}.json", journal)
                self._persist(
                    self._journal(
                        phase="idle",
                        operation_id=None,
                        active=candidate,
                        previous=active,
                        candidate=None,
                    )
                )
            except Exception:
                if mutated:
                    self._restore(active, previous, operation_id)
                raise
            return {
                "manifest_digest": candidate["manifest_digest"],
                "previous_release_id": active["manifest"]["release_id"],
                "release_id": candidate["manifest"]["release_id"],
                "status": "committed",
            }

    def start_active(self) -> dict[str, Any]:
        with self._lock():
            self._recover_locked()
            active = self._load_active()
            self._verify_slot(active, images=False)
            self._prove_local(active)
            self._compose(active)
            self._verify_runtime()
            return {"release_id": active["manifest"]["release_id"], "status": "active"}

    def rollback(self) -> dict[str, Any]:
        with self._lock():
            self._recover_locked()
            active = self._load_active()
            if not self.paths.previous.exists():
                raise ReleaseError("verified previous release is unavailable")
            previous = validate_slot(read_json(self.paths.previous))
            self._verify_slot(previous, images=False)
            self._prove_local(previous)
            operation_id = uuid.uuid4().hex
            journal = self._journal(
                phase="prepared",
                operation_id=operation_id,
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
                self.fault("rollback_after_runtime")
                atomic_write(self.paths.active, previous)
                self.fault("rollback_after_active_write")
                atomic_write(self.paths.previous, active)
                self._persist(
                    self._journal(
                        phase="committed",
                        operation_id=operation_id,
                        active=previous,
                        previous=active,
                        candidate=None,
                    )
                )
            except Exception:
                self._restore(active, previous, operation_id)
                raise
            self.fault("rollback_after_committed")
            self._persist(
                self._journal(
                    phase="idle",
                    operation_id=None,
                    active=previous,
                    previous=active,
                    candidate=None,
                )
            )
            return {
                "release_id": previous["manifest"]["release_id"],
                "previous_release_id": active["manifest"]["release_id"],
                "status": "rolled_back",
            }
