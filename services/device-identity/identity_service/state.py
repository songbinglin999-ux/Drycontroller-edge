from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any


class IdentityStateError(RuntimeError):
    pass


class IdentityStateStore:
    def __init__(self, *, private_dir: Path, active_device_path: Path):
        self.private_dir = private_dir
        self.factory_state_path = private_dir / "factory-state.json"
        self.factory_closed_path = private_dir / "factory-closed.json"
        self.candidate_path = private_dir / "credential-candidate.json"
        self.rotation_candidate_path = private_dir / "rotation-candidate.json"
        self.tailnet_join_attempt_path = private_dir / "tailnet-join-attempt.json"
        self.active_device_path = active_device_path

    def initialize(self) -> dict[str, Any]:
        self._ensure_directories()
        active = self.active()
        if active:
            return {"status": "registered", "device_id": active["device_id"]}
        state = self._read_json(self.factory_state_path)
        if state:
            return state
        state = {
            "schema": "drycontroller.factory-identity-state.v1",
            "status": "unregistered",
            "request_id": secrets.token_hex(16),
            "first_boot_nonce": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
        }
        self._write_json(self.factory_state_path, state)
        return state

    def candidate(self) -> dict[str, Any] | None:
        return self._read_json(self.candidate_path)

    def create_candidate(
        self,
        *,
        request_id: str,
        device_id: str,
        operation: str = "activate",
    ) -> dict[str, Any]:
        existing = self.candidate()
        if existing:
            if (
                existing.get("request_id") != request_id
                or existing.get("device_id") != device_id
                or existing.get("operation", "activate") != operation
            ):
                raise IdentityStateError("a different activation candidate already exists")
            return existing
        credential = secrets.token_urlsafe(48)
        candidate = {
            "schema": "drycontroller.runtime-credential-candidate.v1",
            "request_id": request_id,
            "device_id": device_id,
            "operation": operation,
            "runtime_credential": credential,
            "runtime_credential_hash": hashlib.sha256(credential.encode("utf-8")).hexdigest(),
        }
        self._write_json(self.candidate_path, candidate)
        return candidate

    def activate(
        self,
        *,
        request_id: str,
        device_id: str,
        tenant_id: str,
        line_id: str,
        node_name: str,
        identity_fingerprint: str,
        runtime_credential_hash: str,
        credential_expires_at: str,
    ) -> dict[str, Any]:
        existing = self.active()
        if existing:
            if (
                existing.get("request_id") == request_id
                and existing.get("device_id") == device_id
                and existing.get("runtime_credential_hash") == runtime_credential_hash
            ):
                return existing
            raise IdentityStateError("device identity is already active")
        candidate = self.candidate()
        if (
            not candidate
            or candidate.get("request_id") != request_id
            or candidate.get("device_id") != device_id
            or candidate.get("runtime_credential_hash") != runtime_credential_hash
        ):
            raise IdentityStateError("activation confirmation does not match the candidate")
        active = {
            "schema": "drycontroller.device-identity.v1",
            "device_id": device_id,
            "tenant_id": tenant_id,
            "line_id": line_id,
            "node_name": node_name,
            "identity_fingerprint": identity_fingerprint,
            "runtime_credential": candidate["runtime_credential"],
            "runtime_credential_hash": runtime_credential_hash,
            "credential_expires_at": credential_expires_at,
            "request_id": request_id,
        }
        self._write_json(self.active_device_path, active)
        self.candidate_path.unlink(missing_ok=True)
        self._write_json(
            self.factory_state_path,
            {
                "schema": "drycontroller.factory-identity-state.v1",
                "status": "registered",
                "request_id": request_id,
                "device_id": device_id,
            },
        )
        return active

    def active(self) -> dict[str, Any] | None:
        return self._read_json(self.active_device_path)

    def bind_tailnet_node_id(self, node_id: str) -> dict[str, Any]:
        active = self.active()
        if not active or not node_id:
            raise IdentityStateError("tailnet node identity is unavailable")
        existing = active.get("tailnet_node_id")
        if existing and existing != node_id:
            raise IdentityStateError("tailnet node identity cannot be replaced")
        if not existing:
            active = {**active, "tailnet_node_id": node_id}
            self._write_json(self.active_device_path, active)
        self.clear_tailnet_join_attempt()
        return active

    def tailnet_join_attempt(self) -> dict[str, Any] | None:
        return self._read_json(self.tailnet_join_attempt_path)

    def begin_tailnet_join(
        self,
        *,
        node_name: str,
        expected_version: str,
        login_server: str,
    ) -> dict[str, Any]:
        active = self.active()
        if not active or active.get("node_name") != node_name or active.get("tailnet_node_id"):
            raise IdentityStateError("tailnet join attempt conflicts with active identity")
        attempt = {
            "schema": "drycontroller.tailnet-join-attempt.v1",
            "device_id": active["device_id"],
            "request_id": active["request_id"],
            "node_name": node_name,
            "expected_version": expected_version,
            "login_server": login_server,
        }
        existing = self.tailnet_join_attempt()
        if existing and existing != attempt:
            raise IdentityStateError("tailnet join attempt conflicts with active identity")
        if not existing:
            self._write_json(self.tailnet_join_attempt_path, attempt)
        return attempt

    def clear_tailnet_join_attempt(self) -> None:
        try:
            self.tailnet_join_attempt_path.unlink()
        except FileNotFoundError:
            return
        directory_fd = os.open(self.tailnet_join_attempt_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def factory_closed(self) -> dict[str, Any] | None:
        return self._read_json(self.factory_closed_path)

    def close_factory_endpoint(self) -> dict[str, Any]:
        active = self.active()
        if not active:
            raise IdentityStateError("device identity is not active")
        marker = {
            "schema": "drycontroller.factory-identity-closed.v1",
            "request_id": active["request_id"],
            "device_id": active["device_id"],
        }
        existing = self.factory_closed()
        if existing and existing != marker:
            raise IdentityStateError("factory identity close marker conflicts with active identity")
        if not existing:
            self._write_json(self.factory_closed_path, marker)
        return marker

    def rotation_candidate(self) -> dict[str, Any] | None:
        return self._read_json(self.rotation_candidate_path)

    def create_rotation_candidate(self) -> dict[str, Any]:
        active = self.active()
        if not active:
            raise IdentityStateError("device identity is not active")
        existing = self.rotation_candidate()
        if existing:
            if active.get("runtime_credential_hash") == existing.get("runtime_credential_hash"):
                self.rotation_candidate_path.unlink(missing_ok=True)
                return {**existing, "already_promoted": True}
            if (
                existing.get("device_id") != active.get("device_id")
                or existing.get("identity_fingerprint") != active.get("identity_fingerprint")
                or existing.get("previous_runtime_credential_hash") != active.get("runtime_credential_hash")
            ):
                raise IdentityStateError("rotation candidate does not match the active identity")
            return existing
        credential = secrets.token_urlsafe(48)
        candidate = {
            "schema": "drycontroller.runtime-credential-rotation.v1",
            "request_id": secrets.token_hex(16),
            "device_id": active["device_id"],
            "identity_fingerprint": active["identity_fingerprint"],
            "previous_runtime_credential_hash": active["runtime_credential_hash"],
            "runtime_credential": credential,
            "runtime_credential_hash": hashlib.sha256(credential.encode("utf-8")).hexdigest(),
        }
        self._write_json(self.rotation_candidate_path, candidate)
        return candidate

    def promote_rotation(
        self,
        *,
        request_id: str,
        runtime_credential_hash: str,
        credential_expires_at: str,
    ) -> dict[str, Any]:
        active = self.active()
        candidate = self.rotation_candidate()
        if not active or not candidate:
            raise IdentityStateError("rotation state is unavailable")
        if (
            candidate.get("request_id") != request_id
            or candidate.get("runtime_credential_hash") != runtime_credential_hash
            or candidate.get("device_id") != active.get("device_id")
            or candidate.get("identity_fingerprint") != active.get("identity_fingerprint")
            or candidate.get("previous_runtime_credential_hash") != active.get("runtime_credential_hash")
        ):
            raise IdentityStateError("rotation confirmation does not match the candidate")
        rotated = {
            **active,
            "runtime_credential": candidate["runtime_credential"],
            "runtime_credential_hash": runtime_credential_hash,
            "credential_expires_at": credential_expires_at,
            "credential_request_id": request_id,
        }
        self._write_json(self.active_device_path, rotated)
        self.rotation_candidate_path.unlink(missing_ok=True)
        return rotated

    def _ensure_directories(self) -> None:
        self.private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.active_device_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.private_dir, 0o700)
        os.chmod(self.active_device_path.parent, 0o700)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IdentityStateError(f"identity state is invalid: {path.name}") from exc
        if not isinstance(data, dict):
            raise IdentityStateError(f"identity state is invalid: {path.name}")
        return data

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        self._ensure_directories()
        content = (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
