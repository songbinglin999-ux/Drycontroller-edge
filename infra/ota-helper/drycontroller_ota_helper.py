#!/usr/bin/python3
"""Root-owned, operation-ID-only OTA execution boundary."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import signal
import socketserver
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from release_engine import ReleaseError, validate_release_journal


SOCKET_PATH = Path("/run/drycontroller-ota/helper.sock")
STATE_PATH = Path("/var/lib/drycontroller-ota/operations.json")
REQUEST_PATH = Path("/var/lib/drycontroller-ota/request.json")
RELEASE_JOURNAL_PATH = Path("/var/lib/drycontroller-ota/release-journal.json")
LOG_PATH = Path("/var/log/drycontroller-ota/operations.log")
APPLY_PROGRAM = Path("/opt/drycontroller-host/current/infra/ota-helper/ota-apply")
PROJECT_DIR = Path("/opt/Drycontroller-edge")
OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
TERMINAL_STATUSES = {"succeeded", "failed"}
APPLY_TIMEOUT_SECONDS = 30 * 60
PROCESS_TERMINATE_GRACE_SECONDS = 10
PROCESS_OUTPUT_DRAIN_GRACE_SECONDS = 10
MAX_FULL_OPERATIONS = 256
MAX_OPERATION_TOMBSTONES = 4096
MAX_OPERATION_LOG_BYTES = 1024 * 1024
LOG_FILE_MAX_BYTES = 16 * 1024 * 1024
LOG_ENTRY_OVERHEAD_BYTES = 512
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


def operation_sequence(operation_id: str) -> int:
    """Return the Cloud job sequence encoded in the first 64 bits."""
    return int(validate_operation_id(operation_id)[:16], 16)


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
        request_path: Path = REQUEST_PATH,
        release_journal_path: Path = RELEASE_JOURNAL_PATH,
        log_path: Path = LOG_PATH,
        apply_program: Path = APPLY_PROGRAM,
        project_dir: Path = PROJECT_DIR,
    ) -> None:
        self.state_path = state_path
        self.request_path = request_path
        self.release_journal_path = release_journal_path
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
            self._write_state({"operations": {}, "tombstones": {}, "high_watermark": -1})
        if not self.log_path.exists():
            self.log_path.touch(mode=0o600)
        self.log_path.chmod(0o600)

    def _read_state(self) -> dict[str, Any]:
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("operations"), dict):
            raise RuntimeError("invalid OTA helper state")
        tombstones = data.setdefault("tombstones", {})
        if not isinstance(tombstones, dict):
            raise RuntimeError("invalid OTA helper state")
        high_watermark = data.setdefault("high_watermark", -1)
        if not isinstance(high_watermark, int) or high_watermark < -1:
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

    def _write_request(self, operation_id: str) -> None:
        payload = json.dumps(
            {"operation_id": validate_operation_id(operation_id)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        temporary = self.request_path.with_name("request.json.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.request_path)
            self.request_path.chmod(0o600)
            directory_fd = os.open(self.request_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def _request_operation_id(self) -> str | None:
        try:
            request = json.loads(self.request_path.read_text(encoding="utf-8"))
            return operation_id_from_body(request)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, InvalidOperationId):
            return None

    def _release_journal_idle(self) -> bool:
        try:
            journal = json.loads(self.release_journal_path.read_text(encoding="utf-8"))
            journal = validate_release_journal(journal)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ReleaseError):
            return False
        return journal["phase"] == "idle"

    def _recover_interrupted_operation(self) -> None:
        state = self._read_state()
        interrupted = [
            operation
            for operation in state["operations"].values()
            if operation.get("status") in {"queued", "running"}
        ]
        if len(interrupted) == 1:
            operation_id = interrupted[0].get("operation_id")
            if isinstance(operation_id, str) and self._request_operation_id() == operation_id:
                self._active_operation_id = operation_id
                worker = threading.Thread(target=self._run, args=(operation_id,), daemon=True)
                worker.start()
                return
        changed = False
        log_end = self.log_path.stat().st_size
        for operation in interrupted:
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
            self._prune_state(state)
            self._write_state(state)

    def _prune_state(self, state: dict[str, Any]) -> None:
        operations = state["operations"]
        tombstones = state["tombstones"]
        while len(operations) > MAX_FULL_OPERATIONS:
            terminal = [
                operation
                for operation in operations.values()
                if operation.get("status") in TERMINAL_STATUSES
            ]
            if not terminal:
                break
            oldest = min(terminal, key=lambda operation: str(operation.get("finished_at") or ""))
            operation_id = oldest["operation_id"]
            tombstones[operation_id] = {
                "operation_id": operation_id,
                "status": oldest["status"],
                "step": "retained",
                "started_at": oldest.get("started_at"),
                "finished_at": oldest.get("finished_at"),
                "exit_code": oldest.get("exit_code"),
                "error": oldest.get("error"),
                "log_available": False,
            }
            del operations[operation_id]

        if len(tombstones) > MAX_OPERATION_TOMBSTONES:
            oldest_ids = sorted(
                tombstones,
                key=lambda operation_id: str(tombstones[operation_id].get("finished_at") or ""),
            )
            for operation_id in oldest_ids[: len(tombstones) - MAX_OPERATION_TOMBSTONES]:
                del tombstones[operation_id]

    def _prepare_log_for_new_operation(self, state: dict[str, Any]) -> int:
        reserve = MAX_OPERATION_LOG_BYTES + LOG_ENTRY_OVERHEAD_BYTES
        if self.log_path.stat().st_size <= max(0, LOG_FILE_MAX_BYTES - reserve):
            return self.log_path.stat().st_size

        for operation in state["operations"].values():
            if operation.get("status") in TERMINAL_STATUSES:
                operation.update({"log_available": False, "log_start": None, "log_end": None})
        self._write_state(state)
        # The persisted metadata must become conservative before old bytes are
        # removed; a crash can leave logs unavailable, never falsely readable.
        with self.log_path.open("wb") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        return 0

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
            if existing is None:
                existing = state["tombstones"].get(operation_id)
            if existing is not None:
                if (
                    existing.get("status") == "queued"
                    and existing.get("step") == "recovery_pending"
                    and self._active_operation_id is None
                    and self._request_operation_id() == operation_id
                ):
                    self._active_operation_id = operation_id
                    worker = threading.Thread(target=self._run, args=(operation_id,), daemon=True)
                    worker.start()
                return dict(existing), False
            persisted_active = next(
                (
                    operation.get("operation_id")
                    for operation in state["operations"].values()
                    if operation.get("status") in {"queued", "running"}
                ),
                None,
            )
            if isinstance(persisted_active, str):
                raise OperationBusy(persisted_active)
            if self._active_operation_id is not None:
                raise OperationBusy(self._active_operation_id)
            sequence = operation_sequence(operation_id)
            if sequence <= state["high_watermark"]:
                return {
                    "operation_id": operation_id,
                    "status": "failed",
                    "step": "retained",
                    "started_at": None,
                    "finished_at": None,
                    "exit_code": None,
                    "error": "operation_replay_rejected",
                    "log_available": False,
                }, False

            log_start = self._prepare_log_for_new_operation(state)
            self._write_request(operation_id)
            operation = {
                "operation_id": operation_id,
                "status": "queued",
                "step": "queued",
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                "error": None,
                "log_available": True,
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
            state = self._read_state()
            operation = state["operations"].get(operation_id)
            if operation is None:
                operation = state["tombstones"].get(operation_id)
            if operation is None:
                raise OperationNotFound(operation_id)
            return dict(operation)

    def logs(self, operation_id: str) -> dict[str, Any]:
        operation_id = validate_operation_id(operation_id)
        with self._lock:
            state = self._read_state()
            operation = state["operations"].get(operation_id)
            if operation is None:
                operation = state["tombstones"].get(operation_id)
            if operation is None:
                raise OperationNotFound(operation_id)
            if not operation.get("log_available", True):
                return {
                    "success": True,
                    "operation_id": operation_id,
                    "count": 0,
                    "data": [],
                    "available": False,
                }
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
            "available": True,
        }

    def _patch(
        self,
        operation_id: str,
        *,
        prune: bool = False,
        advance_high_watermark: bool = False,
        **values: Any,
    ) -> None:
        state = self._read_state()
        state["operations"][operation_id].update(values)
        if advance_high_watermark:
            state["high_watermark"] = max(
                state["high_watermark"], operation_sequence(operation_id)
            )
        if prune:
            self._prune_state(state)
        self._write_state(state)

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
                    marker = b"\n[ota-helper] process output truncated\n"
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
            except ProcessLookupError:
                pass
            except OSError:
                pass
            reader.join(timeout=PROCESS_OUTPUT_DRAIN_GRACE_SECONDS)
            if not reader.is_alive():
                return True
        return False

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
                operation = self._read_state()["operations"][operation_id]
                log_limit = (
                    int(operation["log_start"])
                    + MAX_OPERATION_LOG_BYTES
                    + LOG_ENTRY_OVERHEAD_BYTES
                )
            with self.log_path.open("ab", buffering=0) as log_handle:
                for attempt in range(2):
                    process: subprocess.Popen[bytes] | None = None
                    reader: threading.Thread | None = None
                    reader_errors: list[Exception] = []
                    error = None
                    self._write_log_bounded(
                        log_handle,
                        b"[ota-helper] fixed state machine started\n",
                        log_limit,
                    )
                    try:
                        process = subprocess.Popen(
                            [str(self.apply_program)],
                            cwd=self.project_dir,
                            env=FIXED_ENV,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            close_fds=True,
                            start_new_session=True,
                        )
                        reader = threading.Thread(
                            target=self._copy_process_output,
                            args=(process, log_handle, reader_errors, log_limit),
                            daemon=True,
                        )
                        reader.start()
                        try:
                            exit_code = process.wait(timeout=APPLY_TIMEOUT_SECONDS)
                        except subprocess.TimeoutExpired:
                            error = "fixed_state_machine_timeout"
                            self._terminate_process(process)
                            exit_code = process.returncode
                        if exit_code not in {None, 0}:
                            self._terminate_process(process)
                        if not self._finish_output_reader(process, reader) and error is None:
                            error = "fixed_state_machine_log_drain_timeout"
                        if reader_errors and error is None:
                            error = f"fixed_state_machine_log_error:{type(reader_errors[0]).__name__}"
                    except (OSError, subprocess.SubprocessError) as exc:
                        error = f"fixed_state_machine_error:{type(exc).__name__}"
                        if process is not None and process.poll() is None:
                            self._terminate_process(process)
                        if process is not None and reader is not None:
                            self._finish_output_reader(process, reader)
                    self._write_log_bounded(
                        log_handle,
                        f"[ota-helper] fixed state machine exited: {exit_code}\n".encode(),
                        log_limit,
                    )
                    if self._release_journal_idle():
                        break
                    if attempt == 0:
                        self._write_log_bounded(
                            log_handle,
                            b"[ota-helper] unfinished release detected; starting recovery\n",
                            log_limit,
                        )
                        with self._lock:
                            self._patch(operation_id, step="recovery")
                log_handle.flush()
                os.fsync(log_handle.fileno())
        except OSError as exc:
            error = f"fixed_state_machine_error:{type(exc).__name__}"

        if not self._release_journal_idle():
            with self._lock:
                self._patch(
                    operation_id,
                    status="queued",
                    step="recovery_pending",
                    exit_code=None,
                    error=error or "fixed_state_machine_recovery_pending",
                )
                self._active_operation_id = None
            return

        release_summary = self._release_summary()
        with self._lock:
            succeeded = exit_code == 0 and error is None
            release_values: dict[str, Any] = {
                "active_release_id": release_summary.get("release_id"),
                "active_release_version": release_summary.get("version"),
                "active_manifest_digest": release_summary.get("manifest_digest"),
            }
            if succeeded:
                release_values["target_version"] = release_summary.get("version")
                release_values["release_id"] = release_summary.get("release_id")
                release_values["manifest_digest"] = release_summary.get("manifest_digest")
            self._patch(
                operation_id,
                prune=True,
                advance_high_watermark=succeeded,
                status="succeeded" if succeeded else "failed",
                step="done",
                finished_at=utc_now(),
                exit_code=exit_code,
                error=error if error is not None else (None if succeeded else "fixed_state_machine_failed"),
                log_end=self.log_path.stat().st_size,
                **release_values,
            )
            self._active_operation_id = None

    def _release_summary(self) -> dict[str, str | None]:
        try:
            journal = json.loads(self.release_journal_path.read_text(encoding="utf-8"))
            active = journal.get("active") if isinstance(journal, dict) else None
            manifest = active.get("manifest") if isinstance(active, dict) else None
            digest = active.get("manifest_digest") if isinstance(active, dict) else None
            if not isinstance(manifest, dict):
                return {}
            return {
                "release_id": str(manifest.get("release_id") or "") or None,
                "version": str(manifest.get("version") or "") or None,
                "manifest_digest": str(digest or "") or None,
            }
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}


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
