#!/usr/bin/python3
"""Root-owned operation-ID-only boundary for immutable HostRelease changes."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import socketserver
import subprocess
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SOCKET_PATH = Path("/run/drycontroller-host-release/runner.sock")
STATE_PATH = Path("/var/lib/drycontroller-host/operations.json")
REQUEST_PATH = Path("/var/lib/drycontroller-host/request.json")
LOG_PATH = Path("/var/log/drycontroller-host/operations.log")
APPLY_PROGRAM = Path("/usr/libexec/drycontroller/host-release-apply")
OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
TERMINAL = {"succeeded", "failed"}
MAX_OPERATIONS = 256
FIXED_ENV = {
    "HOME": "/root",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "DOCKER_CONFIG": "/etc/drycontroller/registry",
    "COMPOSE_PROJECT_NAME": "drycontroller-edge",
}


class InvalidOperation(ValueError):
    pass


class OperationBusy(RuntimeError):
    pass


class OperationMissing(LookupError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def validate_operation_id(value: object) -> str:
    if not isinstance(value, str) or OPERATION_ID.fullmatch(value) is None:
        raise InvalidOperation("operation_id must be exactly 32 lowercase hexadecimal characters")
    return value


def operation_id_from_body(body: object) -> str:
    if not isinstance(body, dict) or set(body) != {"operation_id"}:
        raise InvalidOperation("request body must contain only operation_id")
    return validate_operation_id(body["operation_id"])


class OperationController:
    def __init__(
        self,
        *,
        state_path: Path = STATE_PATH,
        request_path: Path = REQUEST_PATH,
        log_path: Path = LOG_PATH,
        apply_program: Path = APPLY_PROGRAM,
    ) -> None:
        self.state_path = state_path
        self.request_path = request_path
        self.log_path = log_path
        self.apply_program = apply_program
        self._lock = threading.Lock()
        self._active: str | None = None
        self._prepare()
        self._recover()

    def _prepare(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_path.parent, 0o700)
        os.chmod(self.log_path.parent, 0o700)
        if not self.state_path.exists():
            self._write_state({"operations": {}})
        if not self.log_path.exists():
            self.log_path.touch(mode=0o600)
        os.chmod(self.log_path, 0o600)

    def _read_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("invalid HostRelease operation state") from exc
        if not isinstance(state, dict) or not isinstance(state.get("operations"), dict):
            raise RuntimeError("invalid HostRelease operation state")
        return state

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_state(self, state: dict[str, Any]) -> None:
        self._atomic_write(self.state_path, state)

    def _write_request(self, operation_id: str) -> None:
        self._atomic_write(self.request_path, {"operation_id": operation_id})

    def _request_id(self) -> str | None:
        try:
            return operation_id_from_body(json.loads(self.request_path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, InvalidOperation):
            return None

    def _recover(self) -> None:
        state = self._read_state()
        running = [item for item in state["operations"].values() if item.get("status") in {"queued", "running"}]
        if len(running) == 1:
            operation_id = running[0].get("operation_id")
            if isinstance(operation_id, str) and self._request_id() == operation_id:
                self._active = operation_id
                threading.Thread(target=self._run, args=(operation_id,), daemon=True).start()
                return
        for operation in running:
            operation.update(
                {
                    "status": "failed",
                    "finished_at": utc_now(),
                    "error": "host_release_runner_restarted_without_request",
                }
            )
        if running:
            self._write_state(state)

    def start(self, operation_id: str) -> dict[str, Any]:
        operation_id = validate_operation_id(operation_id)
        with self._lock:
            state = self._read_state()
            existing = state["operations"].get(operation_id)
            if existing is not None:
                return dict(existing)
            if self._active is not None:
                raise OperationBusy(self._active)
            operation = {
                "operation_id": operation_id,
                "status": "queued",
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                "error": None,
                "log_start": self.log_path.stat().st_size,
                "log_end": None,
            }
            state["operations"][operation_id] = operation
            terminal = [
                item
                for item in state["operations"].values()
                if item.get("status") in TERMINAL and item.get("operation_id") != operation_id
            ]
            for item in sorted(terminal, key=lambda value: str(value.get("finished_at") or ""))[
                : max(0, len(state["operations"]) - MAX_OPERATIONS)
            ]:
                state["operations"].pop(item["operation_id"], None)
            self._write_request(operation_id)
            self._write_state(state)
            self._active = operation_id
            threading.Thread(target=self._run, args=(operation_id,), daemon=True).start()
            return dict(operation)

    def _run(self, operation_id: str) -> None:
        state = self._read_state()
        operation = state["operations"][operation_id]
        operation.update({"status": "running", "started_at": operation.get("started_at") or utc_now()})
        self._write_state(state)
        exit_code = 1
        error = None
        with self.log_path.open("ab", buffering=0) as log:
            try:
                completed = subprocess.run(
                    [str(self.apply_program)],
                    cwd="/",
                    env=FIXED_ENV,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=60 * 60,
                )
                exit_code = completed.returncode
                if exit_code != 0:
                    error = "host_release_apply_failed"
            except subprocess.TimeoutExpired:
                error = "host_release_apply_timeout"
            except OSError:
                error = "host_release_apply_unavailable"
            log.flush()
            os.fsync(log.fileno())
        with self._lock:
            state = self._read_state()
            operation = state["operations"][operation_id]
            operation.update(
                {
                    "status": "succeeded" if exit_code == 0 else "failed",
                    "finished_at": utc_now(),
                    "exit_code": exit_code,
                    "error": error,
                    "log_end": self.log_path.stat().st_size,
                }
            )
            self._write_state(state)
            self._active = None

    def get(self, operation_id: str) -> dict[str, Any]:
        operation_id = validate_operation_id(operation_id)
        operation = self._read_state()["operations"].get(operation_id)
        if operation is None:
            raise OperationMissing(operation_id)
        return dict(operation)

    def logs(self, operation_id: str) -> dict[str, Any]:
        operation = self.get(operation_id)
        start = operation.get("log_start")
        end = operation.get("log_end") or self.log_path.stat().st_size
        if not isinstance(start, int) or not isinstance(end, int) or end < start or end - start > 1024 * 1024:
            raise RuntimeError("HostRelease operation log bounds invalid")
        with self.log_path.open("rb") as handle:
            handle.seek(start)
            data = handle.read(end - start)
        return {
            "operation_id": operation_id,
            "available": True,
            "data": data.decode("utf-8", errors="replace").splitlines(),
        }


class ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/ready":
            self._json(200, {"success": True, "ready": True})
            return
        match = re.fullmatch(r"/host-release/operations/([0-9a-f]{32})(/logs)?", path)
        if match is None:
            self._json(404, {"error": "not_found"})
            return
        try:
            result = self.server.controller.logs(match[1]) if match[2] else self.server.controller.get(match[1])
        except OperationMissing:
            self._json(404, {"error": "host_release_operation_not_found"})
            return
        except (InvalidOperation, RuntimeError):
            self._json(409, {"error": "host_release_operation_invalid"})
            return
        self._json(200, result)

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/host-release/start":
            self._json(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 1024:
                raise InvalidOperation("request body size invalid")
            operation_id = operation_id_from_body(json.loads(self.rfile.read(length)))
            result = self.server.controller.start(operation_id)
        except (InvalidOperation, UnicodeDecodeError, json.JSONDecodeError):
            self._json(422, {"error": "invalid_host_release_operation"})
            return
        except OperationBusy as exc:
            self._json(409, {"error": "host_release_operation_busy", "operation_id": str(exc)})
            return
        self._json(202, result)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _prepare_socket(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o755)
    if path.exists() or path.is_socket():
        path.unlink()


def main() -> None:
    _prepare_socket(SOCKET_PATH)
    controller = OperationController()
    with ThreadingUnixServer(str(SOCKET_PATH), RequestHandler) as server:
        server.controller = controller
        os.chown(SOCKET_PATH, 0, 0)
        os.chmod(SOCKET_PATH, 0o660)
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
