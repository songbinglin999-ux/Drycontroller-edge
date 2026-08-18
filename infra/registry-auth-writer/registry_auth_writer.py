#!/usr/bin/python3
"""Root-owned atomic writer for the Docker auth consumed by release pulls."""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import socketserver
import stat
import struct
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any


SOCKET_PATH = Path("/run/drycontroller-registry-auth/writer.sock")
AUTH_PATH = Path("/var/lib/drycontroller-ota/registry-auth.json")
MAX_REQUEST_BYTES = 16 * 1024
_REGISTRY = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?$")


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
    }:
        raise RegistryAuthError("registry auth payload invalid")
    registry = _normalize_registry(payload["registry_host"])
    credential_id = str(payload["credential_id"] or "").strip()
    username = str(payload["username"] or "").strip()
    secret = str(payload["secret"] or "")
    fingerprint = str(payload["secret_fingerprint"] or "").strip()
    version = payload["version"]
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
    ):
        raise RegistryAuthError("registry auth payload invalid")
    return {
        "credential_id": credential_id,
        "registry_host": registry,
        "username": username,
        "secret": secret,
        "secret_fingerprint": fingerprint,
        "version": version,
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


def install_registry_auth(payload: object, *, path: Path = AUTH_PATH) -> dict[str, Any]:
    normalized = normalize_payload(payload)
    _validate_parent(path)
    _validate_existing(path)
    auth = base64.b64encode(
        f"{normalized['username']}:{normalized['secret']}".encode("utf-8")
    ).decode("ascii")
    encoded = json.dumps(
        {"auths": {normalized["registry_host"]: {"auth": auth}}},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
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
    return {
        "credential_id": normalized["credential_id"],
        "registry_host": normalized["registry_host"],
        "username": normalized["username"],
        "secret_fingerprint": normalized["secret_fingerprint"],
        "version": normalized["version"],
    }


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
