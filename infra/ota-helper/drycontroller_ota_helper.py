#!/usr/bin/python3
"""Root-owned, operation-ID-only OTA execution boundary."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import socketserver
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SOCKET_PATH = Path("/run/drycontroller-ota/helper.sock")
STATE_PATH = Path("/var/lib/drycontroller-ota/operations.json")
LOG_PATH = Path("/var/log/drycontroller-ota/operations.log")
APPLY_PROGRAM = Path("/usr/libexec/drycontroller/ota-apply")
PROJECT_DIR = Path("/opt/Drycontroller-edge")
OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
TERMINAL_STATUSES = {"succeeded", "failed"}
FIXED_ENV = {
    "HOME": "/root",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "DOCKER_CONFIG": "/etc/drycontroller/registry",
    "COMPOSE_PROJECT_NAME": "drycontroller-edge",
}


class InvalidOperationId(ValueError):
    pass


class OperationBusy(RuntimeError):
    def __init__(self, operation_id: str):
        super().__init__(operation_id)
        self.operation_id = operation_id


class OperationNotFound(LookupError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def validate_operation_id(value: object) -> str:
    if not isinstance(value, str) or OPERATION_ID.fullmatch(value) is None:
        raise InvalidOperationId("operation_id must be exactly 32 lowercase hexadecimal characters")
    return value


def operation_id_from_body(body: object) -> str:
    if not isinstance(body, dict) or set(body) != {"operation_id"}:
        raise InvalidOperationId("request body must contain only operation_id")
    return validate_operation_id(body["operation_id"])


class OperationController:
    """Serialize a fixed host state machine and persist idempotency."""

    def __init__(
        self,
        *,
        state_path: Path = STATE_PATH,
        log_path: Path = LOG_PATH,
        apply_program: Path = APPLY_PROGRAM,
        project_dir: Path = PROJECT_DIR,
    ) -> None:
        self.state_path = state_path
        self.log_path = log_path
        self.apply_program = apply_program
        self.project_dir = project_dir
        self._lock = threading.Lock()
        self._active_operation_id: str | None = None
        self._prepare_storage()
        self._recover_interrupted_operation()

    def _prepare_storage(self) -> None:
        for directory in (self.state_path.parent, self.log_path.parent):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
        if not self.state_path.exists():
            self._write_state({"operations": {}})
        if not self.log_path.exists():
            self.log_path.touch(mode=0o600)
        self.log_path.chmod(0o600)

    def _read_state(self) -> dict[str, Any]:
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("operations"), dict):
            raise RuntimeError("invalid OTA helper state")
        return data

    def _write_state(self, data: dict[str, Any]) -> None:
        temporary = self.state_path.with_name("operations.json.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.state_path)
            self.state_path.chmod(0o600)
            directory_fd = os.open(self.state_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _recover_interrupted_operation(self) -> None:
        state = self._read_state()
        changed = False
        log_end = self.log_path.stat().st_size
        for operation in state["operations"].values():
            if operation.get("status") in {"queued", "running"}:
                operation.update(
                    {
                        "status": "failed",
                        "step": "interrupted",
                        "finished_at": utc_now(),
                        "error": "ota_helper_restarted",
                        "log_end": log_end,
                    }
                )
                changed = True
        if changed:
            self._write_state(state)

    def ready(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ready": True,
                "service": "drycontroller-ota-helper",
                "active_operation_id": self._active_operation_id,
            }

    def start(self, operation_id: str) -> tuple[dict[str, Any], bool]:
        operation_id = validate_operation_id(operation_id)
        with self._lock:
            state = self._read_state()
            existing = state["operations"].get(operation_id)
            if existing is not None:
                return dict(existing), False
            if self._active_operation_id is not None:
                raise OperationBusy(self._active_operation_id)

            log_start = self.log_path.stat().st_size
            operation = {
                "operation_id": operation_id,
                "status": "queued",
                "step": "queued",
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                "error": None,
                "log_start": log_start,
                "log_end": None,
            }
            state["operations"][operation_id] = operation
            self._write_state(state)
            self._active_operation_id = operation_id
            worker = threading.Thread(target=self._run, args=(operation_id,), daemon=True)
            worker.start()
            return dict(operation), True

    def get(self, operation_id: str) -> dict[str, Any]:
        operation_id = validate_operation_id(operation_id)
        with self._lock:
            operation = self._read_state()["operations"].get(operation_id)
            if operation is None:
                raise OperationNotFound(operation_id)
            return dict(operation)

    def logs(self, operation_id: str) -> dict[str, Any]:
        operation = self.get(operation_id)
        start = int(operation["log_start"])
        end_value = operation.get("log_end")
        with self.log_path.open("rb") as handle:
            handle.seek(start)
            data = handle.read(None if end_value is None else max(0, int(end_value) - start))
        lines = data.decode("utf-8", errors="replace").splitlines()[-200:]
        return {
            "success": True,
            "operation_id": operation_id,
            "count": len(lines),
            "data": lines,
        }

    def _patch(self, operation_id: str, **values: Any) -> None:
        state = self._read_state()
        state["operations"][operation_id].update(values)
        self._write_state(state)

    def _run(self, operation_id: str) -> None:
        exit_code: int | None = None
        error: str | None = None
        try:
            with self._lock:
                self._patch(
                    operation_id,
                    status="running",
                    step="apply",
                    started_at=utc_now(),
                )
            with self.log_path.open("ab", buffering=0) as log_handle:
                log_handle.write(b"[ota-helper] fixed state machine started\n")
                process = subprocess.Popen(
                    [str(self.apply_program)],
                    cwd=self.project_dir,
                    env=FIXED_ENV,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                )
                exit_code = process.wait()
                log_handle.write(f"[ota-helper] fixed state machine exited: {exit_code}\n".encode())
        except (OSError, subprocess.SubprocessError) as exc:
            error = f"fixed_state_machine_error:{type(exc).__name__}"

        with self._lock:
            succeeded = exit_code == 0 and error is None
            self._patch(
                operation_id,
                status="succeeded" if succeeded else "failed",
                step="done",
                finished_at=utc_now(),
                exit_code=exit_code,
                error=error if error is not None else (None if succeeded else "fixed_state_machine_failed"),
                log_end=self.log_path.stat().st_size,
            )
            self._active_operation_id = None


class ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class OtaRequestHandler(BaseHTTPRequestHandler):
    server_version = "drycontroller-ota-helper"

    @property
    def controller(self) -> OperationController:
        return self.server.controller  # type: ignore[attr-defined,no-any-return]

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status_code: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _operation_route(self, suffix: str = "") -> str | None:
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            return None
        match = re.fullmatch(rf"/ota/operations/([0-9a-f]{{32}}){suffix}", parsed.path)
        return match.group(1) if match else None

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/ready":
            self._json(200, self.controller.ready())
            return
        operation_id = self._operation_route()
        logs_operation_id = self._operation_route("/logs")
        try:
            if operation_id:
                self._json(200, self.controller.get(operation_id))
                return
            if logs_operation_id:
                self._json(200, self.controller.logs(logs_operation_id))
                return
        except OperationNotFound:
            self._json(404, {"error": "operation_not_found"})
            return
        self._json(404, {"error": "route_not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/ota/start":
            self._json(404, {"error": "route_not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "invalid_content_length"})
            return
        if length < 2 or length > 128:
            self._json(400, {"error": "invalid_request_body"})
            return
        try:
            body = json.loads(self.rfile.read(length))
            operation_id = operation_id_from_body(body)
            operation, created = self.controller.start(operation_id)
        except (json.JSONDecodeError, UnicodeDecodeError, InvalidOperationId) as exc:
            self._json(422, {"error": "invalid_operation_id", "detail": str(exc)})
            return
        except OperationBusy as exc:
            self._json(409, {"error": "operation_in_progress", "operation_id": exc.operation_id})
            return
        self._json(202 if created else 200, operation)


def _prepare_socket(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o755)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != 0:
        raise RuntimeError(f"refusing unsafe existing socket path: {path}")
    path.unlink()


def main() -> None:
    _prepare_socket(SOCKET_PATH)
    controller = OperationController()
    with ThreadingUnixServer(str(SOCKET_PATH), OtaRequestHandler) as server:
        server.controller = controller  # type: ignore[attr-defined]
        os.chmod(SOCKET_PATH, 0o660)
        server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()
