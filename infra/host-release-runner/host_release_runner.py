#!/usr/bin/python3
"""Root-owned operation-ID-only boundary for immutable HostRelease changes."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import signal
import socketserver
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from common.tailscale_status import read_tailscale_status


SOCKET_PATH = Path("/run/drycontroller-host-release/runner.sock")
STATE_PATH = Path("/var/lib/drycontroller-host/operations.json")
REQUEST_PATH = Path("/var/lib/drycontroller-host/request.json")
LOG_PATH = Path("/var/log/drycontroller-host/operations.log")
COORDINATOR_JOURNAL_PATH = Path("/var/lib/drycontroller-host/coordinator-journal.json")
APPLY_PROGRAM = Path("/usr/libexec/drycontroller/host-release-apply")
OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
TERMINAL = {"succeeded", "failed"}
MAX_OPERATIONS = 256
APPLY_TIMEOUT_SECONDS = 60 * 60
PROCESS_TERMINATE_GRACE_SECONDS = 10
PROCESS_OUTPUT_DRAIN_GRACE_SECONDS = 10
MAX_OPERATION_LOG_BYTES = 1024 * 1024
LOG_FILE_MAX_BYTES = 16 * 1024 * 1024
LOG_ENTRY_OVERHEAD_BYTES = 512
COORDINATOR_JOURNAL_SCHEMA = "drycontroller.host-release-coordinator.v1"
COORDINATOR_JOURNAL_FIELDS = {
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
        coordinator_journal_path: Path = COORDINATOR_JOURNAL_PATH,
        log_path: Path = LOG_PATH,
        apply_program: Path = APPLY_PROGRAM,
    ) -> None:
        self.state_path = state_path
        self.request_path = request_path
        self.coordinator_journal_path = coordinator_journal_path
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

    def _prepare_log_for_new_operation(self, state: dict[str, Any]) -> int:
        reserve = MAX_OPERATION_LOG_BYTES + LOG_ENTRY_OVERHEAD_BYTES
        current_size = self.log_path.stat().st_size
        if current_size <= max(0, LOG_FILE_MAX_BYTES - reserve):
            return current_size

        for operation in state["operations"].values():
            if operation.get("status") in TERMINAL:
                operation.update({"log_available": False, "log_start": None, "log_end": None})
        self._write_state(state)
        # Persist conservative metadata before removing bytes so a crash can
        # make logs unavailable, never associate stale bytes with an operation.
        with self.log_path.open("wb") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        return 0

    def _request_id(self) -> str | None:
        try:
            return operation_id_from_body(json.loads(self.request_path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, InvalidOperation):
            return None

    def _coordinator_journal_idle(self) -> bool:
        try:
            journal = json.loads(self.coordinator_journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        return (
            isinstance(journal, dict)
            and set(journal) == COORDINATOR_JOURNAL_FIELDS
            and journal.get("schema") == COORDINATOR_JOURNAL_SCHEMA
            and journal.get("phase") == "idle"
            and isinstance(journal.get("high_watermark"), int)
        )

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
                if (
                    existing.get("status") == "queued"
                    and existing.get("error") == "host_release_recovery_pending"
                    and self._active is None
                    and self._request_id() == operation_id
                ):
                    self._active = operation_id
                    threading.Thread(target=self._run, args=(operation_id,), daemon=True).start()
                return dict(existing)
            persisted_active = next(
                (
                    item.get("operation_id")
                    for item in state["operations"].values()
                    if item.get("status") in {"queued", "running"}
                ),
                None,
            )
            if isinstance(persisted_active, str):
                raise OperationBusy(persisted_active)
            if self._active is not None:
                raise OperationBusy(self._active)
            log_start = self._prepare_log_for_new_operation(state)
            operation = {
                "operation_id": operation_id,
                "status": "queued",
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                "error": None,
                "log_available": True,
                "log_start": log_start,
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

    @staticmethod
    def _process_group_exists(group_id: int) -> bool:
        try:
            os.killpg(group_id, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        group_id = process.pid
        try:
            os.killpg(group_id, signal.SIGTERM)
        except ProcessLookupError:
            if process.poll() is None:
                process.wait()
            return
        except OSError:
            process.terminate()

        deadline = time.monotonic() + PROCESS_TERMINATE_GRACE_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is None:
                try:
                    process.wait(timeout=min(0.05, max(0.01, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
            if not OperationController._process_group_exists(group_id):
                return
            time.sleep(0.01)

        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            if process.poll() is None:
                process.kill()
        if process.poll() is None:
            process.wait()

    @staticmethod
    def _write_log_bounded(log_handle: Any, data: bytes, log_limit: int) -> None:
        remaining = max(0, log_limit - log_handle.tell())
        if remaining:
            log_handle.write(data[:remaining])

    @staticmethod
    def _copy_process_output(
        process: subprocess.Popen[bytes],
        log_handle: Any,
        errors: list[Exception],
        log_limit: int,
    ) -> None:
        assert process.stdout is not None
        truncated = False
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                remaining = max(0, log_limit - log_handle.tell())
                if len(chunk) <= remaining:
                    log_handle.write(chunk)
                    continue
                if not truncated and remaining:
                    marker = b"\n[host-release-runner] process output truncated\n"
                    if remaining >= len(marker):
                        log_handle.write(chunk[: remaining - len(marker)])
                        log_handle.write(marker)
                    else:
                        log_handle.write(chunk[:remaining])
                    truncated = True
        except (OSError, ValueError) as exc:
            errors.append(exc)
        finally:
            process.stdout.close()

    @staticmethod
    def _finish_output_reader(process: subprocess.Popen[bytes], reader: threading.Thread) -> bool:
        reader.join(timeout=PROCESS_OUTPUT_DRAIN_GRACE_SECONDS)
        if not reader.is_alive():
            return True

        for stop_signal in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, stop_signal)
            except (ProcessLookupError, OSError):
                pass
            reader.join(timeout=PROCESS_OUTPUT_DRAIN_GRACE_SECONDS)
            if not reader.is_alive():
                return True
        return False

    def _run(self, operation_id: str) -> None:
        state = self._read_state()
        operation = state["operations"][operation_id]
        operation.update({"status": "running", "started_at": operation.get("started_at") or utc_now()})
        self._write_state(state)
        log_limit = int(operation["log_start"]) + MAX_OPERATION_LOG_BYTES + LOG_ENTRY_OVERHEAD_BYTES
        exit_code = 1
        error = None
        with self.log_path.open("ab", buffering=0) as log:
            for attempt in range(2):
                error = None
                process: subprocess.Popen[bytes] | None = None
                reader: threading.Thread | None = None
                reader_errors: list[Exception] = []
                try:
                    process = subprocess.Popen(
                        [str(self.apply_program)],
                        cwd="/",
                        env=FIXED_ENV,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        close_fds=True,
                        start_new_session=True,
                    )
                    reader = threading.Thread(
                        target=self._copy_process_output,
                        args=(process, log, reader_errors, log_limit),
                        daemon=True,
                    )
                    try:
                        reader.start()
                    except RuntimeError:
                        reader = None
                        raise
                    exit_code = process.wait(timeout=APPLY_TIMEOUT_SECONDS)
                    if exit_code != 0:
                        error = "host_release_apply_failed"
                except subprocess.TimeoutExpired:
                    error = "host_release_apply_timeout"
                    assert process is not None
                except RuntimeError:
                    error = "host_release_apply_log_reader_unavailable"
                except OSError:
                    error = "host_release_apply_unavailable"
                if process is not None and error is not None:
                    self._terminate_process(process)
                    exit_code = process.returncode if process.returncode is not None else 1
                if process is not None and reader is not None:
                    if not self._finish_output_reader(process, reader) and error is None:
                        error = "host_release_apply_log_drain_timeout"
                    if reader_errors and error is None:
                        error = f"host_release_apply_log_error:{type(reader_errors[0]).__name__}"
                if process is not None and error is not None:
                    self._terminate_process(process)
                    exit_code = process.returncode if process.returncode is not None else 1
                    if reader is None and process.stdout is not None and not process.stdout.closed:
                        process.stdout.close()
                if self._coordinator_journal_idle():
                    break
                if attempt == 0:
                    self._write_log_bounded(
                        log,
                        b"[host-release-runner] unfinished transaction detected; starting recovery\n",
                        log_limit,
                    )
            log.flush()
            os.fsync(log.fileno())
        if not self._coordinator_journal_idle():
            with self._lock:
                state = self._read_state()
                operation = state["operations"][operation_id]
                operation.update(
                    {
                        "status": "queued",
                        "finished_at": None,
                        "exit_code": None,
                        "error": "host_release_recovery_pending",
                    }
                )
                self._write_state(state)
                self._active = None
            return
        with self._lock:
            state = self._read_state()
            operation = state["operations"][operation_id]
            succeeded = exit_code == 0 and error is None
            operation.update(
                {
                    "status": "succeeded" if succeeded else "failed",
                    "finished_at": utc_now(),
                    "exit_code": exit_code,
                    "error": error if error is not None else (None if succeeded else "host_release_apply_failed"),
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
        operation_id = validate_operation_id(operation_id)
        with self._lock:
            operation = self._read_state()["operations"].get(operation_id)
            if operation is None:
                raise OperationMissing(operation_id)
            if not operation.get("log_available", True):
                return {"operation_id": operation_id, "available": False, "data": []}
            start = operation.get("log_start")
            end_value = operation.get("log_end")
            end = self.log_path.stat().st_size if end_value is None else end_value
            if (
                not isinstance(start, int)
                or not isinstance(end, int)
                or start < 0
                or end < start
                or end > self.log_path.stat().st_size
                or end - start > MAX_OPERATION_LOG_BYTES + LOG_ENTRY_OVERHEAD_BYTES
            ):
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
        if path == "/tailnet/status":
            self._json(200, read_tailscale_status())
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
