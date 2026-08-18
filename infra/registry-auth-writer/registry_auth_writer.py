#!/usr/bin/python3
"""Root-owned atomic writer for the Docker auth consumed by release pulls."""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import socket
import socketserver
import stat
import struct
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any


SOCKET_PATH = Path("/run/drycontroller-registry-auth/writer.sock")
AUTH_PATH = Path("/var/lib/drycontroller-ota/registry-auth.json")
MAX_REQUEST_BYTES = 16 * 1024
DOCUMENT_SCHEMA = "drycontroller.registry-auth.v2"
_REGISTRY = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?$")
_TRANSACTION_ID = re.compile(r"^[0-9a-f]{32}$")
_INSTALL_LOCK = threading.Lock()


class RegistryAuthError(ValueError):
    pass


def _normalize_registry(value: object) -> str:
    registry = str(value or "").strip().rstrip("/")
    for prefix in ("https://", "http://"):
        if registry.startswith(prefix):
            registry = registry[len(prefix) :]
            break
    registry = registry.rstrip("/")
    if _REGISTRY.fullmatch(registry) is None:
        raise RegistryAuthError("registry auth payload invalid")
    return registry


def normalize_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "credential_id",
        "registry_host",
        "username",
        "secret",
        "secret_fingerprint",
        "version",
        "transaction_id",
        "audit_id",
    }:
        raise RegistryAuthError("registry auth payload invalid")
    registry = _normalize_registry(payload["registry_host"])
    credential_id = str(payload["credential_id"] or "").strip()
    username = str(payload["username"] or "").strip()
    secret = str(payload["secret"] or "")
    fingerprint = str(payload["secret_fingerprint"] or "").strip()
    version = payload["version"]
    transaction_id = str(payload["transaction_id"] or "")
    audit_id = payload["audit_id"]
    if (
        not credential_id
        or not username
        or not secret
        or not fingerprint
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
        or len(credential_id) > 256
        or len(username.encode("utf-8")) > 1024
        or len(secret.encode("utf-8")) > 3072
        or len(fingerprint) > 256
        or _TRANSACTION_ID.fullmatch(transaction_id) is None
        or isinstance(audit_id, bool)
        or not isinstance(audit_id, int)
        or audit_id < 1
    ):
        raise RegistryAuthError("registry auth payload invalid")
    return {
        "credential_id": credential_id,
        "registry_host": registry,
        "username": username,
        "secret": secret,
        "secret_fingerprint": fingerprint,
        "version": version,
        "transaction_id": transaction_id,
        "audit_id": audit_id,
    }


def _validate_parent(path: Path) -> None:
    parent = path.parent
    metadata = parent.lstat()
    if (
        parent.resolve(strict=True) != parent
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RegistryAuthError("registry auth destination unavailable")


def _validate_existing(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o400
        or metadata.st_nlink != 1
    ):
        raise RegistryAuthError("registry auth destination unavailable")


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short registry auth write")
        view = view[written:]


def _credential_status(document: object) -> dict[str, Any]:
    if (
        not isinstance(document, dict)
        or set(document) != {"schema", "auths", "credential"}
        or document.get("schema") != DOCUMENT_SCHEMA
    ):
        raise RegistryAuthError("registry auth document invalid")
    auths = document.get("auths")
    credential = document.get("credential")
    if (
        not isinstance(auths, dict)
        or not isinstance(credential, dict)
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
        or set(auths) != {credential["registry_host"]}
        or not isinstance(credential.get("username"), str)
        or not credential["username"]
        or not isinstance(credential.get("secret_fingerprint"), str)
        or not credential["secret_fingerprint"]
        or isinstance(credential.get("version"), bool)
        or not isinstance(credential.get("version"), int)
        or credential["version"] < 1
        or not isinstance(credential.get("transaction_id"), str)
        or _TRANSACTION_ID.fullmatch(credential["transaction_id"]) is None
        or isinstance(credential.get("audit_id"), bool)
        or not isinstance(credential.get("audit_id"), int)
        or credential["audit_id"] < 1
        or not isinstance(credential.get("updated_at"), str)
        or not credential["updated_at"]
    ):
        raise RegistryAuthError("registry auth document invalid")
    entry = auths[credential["registry_host"]]
    if not isinstance(entry, dict) or set(entry) != {"auth"} or not isinstance(entry["auth"], str):
        raise RegistryAuthError("registry auth document invalid")
    try:
        decoded = base64.b64decode(entry["auth"], validate=True)
        username, separator, secret = decoded.partition(b":")
        bound_username = username.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise RegistryAuthError("registry auth document invalid") from exc
    if separator != b":" or not secret or bound_username != credential["username"]:
        raise RegistryAuthError("registry auth document invalid")
    return dict(credential)


def read_registry_status(*, path: Path = AUTH_PATH) -> dict[str, Any]:
    descriptor = -1
    try:
        _validate_parent(path)
        metadata = path.lstat()
    except FileNotFoundError:
        return {"status": "missing"}
    except OSError as exc:
        raise RegistryAuthError("registry auth status unavailable") from exc
    try:
        _validate_existing(path)
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        raw = os.read(descriptor, MAX_REQUEST_BYTES + 1)
        if (
            (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or not raw
            or len(raw) > MAX_REQUEST_BYTES
        ):
            raise RegistryAuthError("registry auth status unavailable")
        document = json.loads(raw)
        return _credential_status(document)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RegistryAuthError) as exc:
        raise RegistryAuthError("registry auth status unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def install_registry_auth(payload: object, *, path: Path = AUTH_PATH) -> dict[str, Any]:
    normalized = normalize_payload(payload)
    auth = base64.b64encode(
        f"{normalized['username']}:{normalized['secret']}".encode("utf-8")
    ).decode("ascii")
    status = {
        "status": "active",
        "credential_id": normalized["credential_id"],
        "registry_host": normalized["registry_host"],
        "username": normalized["username"],
        "secret_fingerprint": normalized["secret_fingerprint"],
        "version": normalized["version"],
        "transaction_id": normalized["transaction_id"],
        "audit_id": normalized["audit_id"],
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    encoded = json.dumps(
        {
            "schema": DOCUMENT_SCHEMA,
            "auths": {normalized["registry_host"]: {"auth": auth}},
            "credential": status,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    with _INSTALL_LOCK:
        _validate_parent(path)
        _validate_existing(path)
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o400,
            )
            _write_all(descriptor, encoded)
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise RegistryAuthError("registry auth update failed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
    return status


class ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


class RegistryAuthHandler(BaseHTTPRequestHandler):
    server_version = "drycontroller-registry-auth-writer"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/registry-auth/install":
            self._json(404, {"error": "route_not_found"})
            return
        try:
            if hasattr(socket, "SO_PEERCRED"):
                raw = self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                _pid, uid, _gid = struct.unpack("3i", raw)
                if uid != 0:
                    self._json(403, {"error": "registry_auth_peer_untrusted"})
                    return
            length = int(self.headers.get("Content-Length", "0"))
            if length < 2 or length > MAX_REQUEST_BYTES:
                raise RegistryAuthError("registry auth payload invalid")
            payload = json.loads(self.rfile.read(length))
            result = install_registry_auth(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, RegistryAuthError, ValueError):
            self._json(422, {"error": "registry_auth_update_refused"})
            return
        self._json(200, {"success": True, "credential": result})

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/registry-auth/status":
            self._json(404, {"error": "route_not_found"})
            return
        try:
            result = read_registry_status()
        except RegistryAuthError:
            self._json(503, {"error": "registry_auth_status_unavailable"})
            return
        self._json(200, {"success": True, "credential": result})


def _prepare_socket(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chown(path.parent, 0, 0)
    os.chmod(path.parent, 0o755)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != 0:
        raise RuntimeError("registry auth writer socket path is unsafe")
    path.unlink()


def main() -> None:
    _prepare_socket(SOCKET_PATH)
    with ThreadingUnixServer(str(SOCKET_PATH), RegistryAuthHandler) as server:
        os.chown(SOCKET_PATH, 0, 0)
        os.chmod(SOCKET_PATH, 0o660)
        server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()
