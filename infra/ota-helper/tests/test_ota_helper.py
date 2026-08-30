from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "drycontroller_ota_helper.py"
if str(MODULE_PATH.parent) not in sys.path:
    sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("drycontroller_ota_helper", MODULE_PATH)
assert SPEC and SPEC.loader
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)

from edge_app_release import (  # noqa: E402
    RELEASE_JOURNAL_SCHEMA,
    canonical_manifest_digest,
)


def _idle_journal() -> dict:
    manifest = {
        "schema": "drycontroller.app-release.v2",
        "profile": "edge-app-v1",
        "release_id": "11111111-1111-4111-8111-111111111111",
        "sequence": 1,
        "source_commit": "1" * 40,
        "deployment_digest": "sha256:" + "2" * 64,
        "images": {
            "edge-api": "registry.example.com/drycontroller/edge-api@sha256:" + "3" * 64,
            "edge-daq": "registry.example.com/drycontroller/edge-daq@sha256:" + "4" * 64,
        },
    }
    return {
        "schema": RELEASE_JOURNAL_SCHEMA,
        "phase": "idle",
        "operation_id": None,
        "active": {
            "manifest": manifest,
            "manifest_digest": canonical_manifest_digest(manifest),
            "manifest_signature": "signature",
            "verified_at": "2026-08-13T00:00:00+00:00",
        },
        "candidate": None,
        "previous": None,
        "error": None,
    }


def _controller(tmp_path: Path, script: str) -> helper.OperationController:
    apply_program = tmp_path / "apply"
    apply_program.write_text(f"#!/bin/sh\nset -eu\n{script}\n", encoding="utf-8")
    apply_program.chmod(0o755)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "release-journal.json").write_text(json.dumps(_idle_journal()), encoding="utf-8")
    return helper.OperationController(
        state_path=state_dir / "operations.json",
        request_path=state_dir / "request.json",
        release_journal_path=state_dir / "release-journal.json",
        log_path=tmp_path / "log" / "operations.log",
        apply_program=apply_program,
        project_dir=project_dir,
    )


def _wait_terminal(controller: helper.OperationController, operation_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        operation = controller.get(operation_id)
        if operation["status"] in helper.TERMINAL_STATUSES:
            return operation
        time.sleep(0.01)
    raise AssertionError("operation did not finish")


def _wait_recovery_pending(controller: helper.OperationController, operation_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        operation = controller.get(operation_id)
        if operation.get("step") == "recovery_pending":
            return operation
        time.sleep(0.01)
    raise AssertionError("operation did not retain recovery-pending state")


@pytest.mark.parametrize(
    "value",
    ["", "0" * 31, "0" * 33, "A" * 32, "../" + "0" * 29, "0" * 31 + "/"],
)
def test_operation_id_is_exact_lowercase_hex(value):
    with pytest.raises(helper.InvalidOperationId):
        helper.validate_operation_id(value)


def test_request_body_rejects_every_field_except_operation_id():
    operation_id = "a" * 32
    assert helper.operation_id_from_body({"operation_id": operation_id}) == operation_id
    with pytest.raises(helper.InvalidOperationId):
        helper.operation_id_from_body({"operation_id": operation_id, "compose": "services: {}"})


def test_same_operation_id_is_idempotent_and_program_receives_no_input(tmp_path):
    marker = tmp_path / "calls"
    controller = _controller(tmp_path, f"printf x >> '{marker}'")
    operation_id = "1" * 32

    first, created = controller.start(operation_id)
    second, created_again = controller.start(operation_id)
    finished = _wait_terminal(controller, operation_id)

    assert created is True
    assert created_again is False
    assert first["operation_id"] == second["operation_id"] == operation_id
    assert finished["status"] == "succeeded"
    assert marker.read_text(encoding="utf-8") == "x"


def test_different_operation_is_rejected_while_one_runs(tmp_path):
    controller = _controller(tmp_path, "sleep 0.2")
    controller.start("2" * 32)

    with pytest.raises(helper.OperationBusy) as raised:
        controller.start("3" * 32)

    assert raised.value.operation_id == "2" * 32
    _wait_terminal(controller, "2" * 32)


def test_failure_is_terminal_and_log_uses_one_fixed_file(tmp_path):
    controller = _controller(tmp_path, "echo fixed-output; exit 17")
    operation_id = "4" * 32
    controller.start(operation_id)

    finished = _wait_terminal(controller, operation_id)
    logs = controller.logs(operation_id)

    assert finished["status"] == "failed"
    assert finished["exit_code"] == 17
    assert finished["error"] == "fixed_state_machine_failed"
    assert "fixed-output" in logs["data"]
    assert {path.name for path in (tmp_path / "state").iterdir()} == {
        "operations.json",
        "release-journal.json",
        "request.json",
    }
    state = json.loads((tmp_path / "state" / "operations.json").read_text(encoding="utf-8"))
    assert set(state["operations"]) == {operation_id}


def test_child_sigkill_recovers_non_idle_release_before_terminal_state(tmp_path):
    marker = tmp_path / "calls"
    journal = tmp_path / "state" / "release-journal.json"
    controller = _controller(
        tmp_path,
        f"""
if [ ! -f '{marker}' ]; then
    printf x > '{marker}'
    printf '%s' '{{"phase":"candidate_apply_started"}}' > '{journal}'
    kill -KILL $$
fi
printf x >> '{marker}'
    printf '%s' '{json.dumps(_idle_journal())}' > '{journal}'
exit 1
""",
    )
    operation_id = "00000000000000010000000000000001"

    controller.start(operation_id)
    finished = _wait_terminal(controller, operation_id)

    assert finished["status"] == "failed"
    assert marker.read_text(encoding="utf-8") == "xx"
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "idle"

    duplicate, created = controller.start(operation_id)
    restarted = helper.OperationController(
        state_path=controller.state_path,
        request_path=controller.request_path,
        release_journal_path=controller.release_journal_path,
        log_path=controller.log_path,
        apply_program=controller.apply_program,
        project_dir=controller.project_dir,
    ).get(operation_id)

    assert created is False
    assert duplicate["status"] == restarted["status"] == "failed"
    assert marker.read_text(encoding="utf-8") == "xx"
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "idle"


def test_unfinished_recovery_blocks_new_id_and_same_id_can_resume(tmp_path):
    journal = tmp_path / "state" / "release-journal.json"
    controller = _controller(
        tmp_path,
        f"printf '%s' '{{\"phase\":\"candidate_apply_started\"}}' > '{journal}'; exit 1",
    )
    operation_id = "00000000000000010000000000000001"
    controller.start(operation_id)

    assert _wait_recovery_pending(controller, operation_id)["status"] == "queued"
    with pytest.raises(helper.OperationBusy) as raised:
        controller.start("00000000000000020000000000000001")
    assert raised.value.operation_id == operation_id

    controller.apply_program.write_text(
        f"#!/bin/sh\nprintf '%s' '{json.dumps(_idle_journal())}' > '{journal}'\nexit 1\n",
        encoding="utf-8",
    )
    controller.apply_program.chmod(0o755)
    _, created = controller.start(operation_id)

    assert created is False
    assert _wait_terminal(controller, operation_id)["status"] == "failed"
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "idle"


@pytest.mark.parametrize("journal_body", ["not-json", '{"phase":42}', '{"phase":"idle"}'])
def test_invalid_release_journal_stays_recovery_pending(tmp_path, journal_body):
    journal = tmp_path / "state" / "release-journal.json"
    controller = _controller(tmp_path, f"printf '%s' '{journal_body}' > '{journal}'; exit 1")
    operation_id = "00000000000000010000000000000001"

    controller.start(operation_id)

    assert _wait_recovery_pending(controller, operation_id)["status"] == "queued"
    with pytest.raises(helper.OperationBusy):
        controller.start("00000000000000020000000000000001")


@pytest.mark.parametrize("slot_name", ["active", "candidate", "previous"])
def test_invalid_idle_release_slot_stays_recovery_pending(tmp_path, slot_name):
    invalid_journal = _idle_journal()
    invalid_journal[slot_name] = {}
    journal = tmp_path / "state" / "release-journal.json"
    controller = _controller(
        tmp_path,
        f"printf '%s' '{json.dumps(invalid_journal)}' > '{journal}'; exit 1",
    )
    operation_id = "00000000000000010000000000000001"

    controller.start(operation_id)

    assert _wait_recovery_pending(controller, operation_id)["status"] == "queued"
    with pytest.raises(helper.OperationBusy):
        controller.start("00000000000000020000000000000001")


def test_child_sigkill_terminates_descendant_before_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "PROCESS_TERMINATE_GRACE_SECONDS", 0.05)
    marker = tmp_path / "calls"
    descendant_marker = tmp_path / "descendant"
    journal = tmp_path / "state" / "release-journal.json"
    controller = _controller(
        tmp_path,
        f"""
if [ ! -f '{marker}' ]; then
    printf x > '{marker}'
    printf '%s' '{{"phase":"candidate_apply_started"}}' > '{journal}'
    (trap '' TERM; sleep 1; printf x > '{descendant_marker}') &
    kill -KILL $$
fi
sleep 0.1
test ! -f '{descendant_marker}'
printf '%s' '{json.dumps(_idle_journal())}' > '{journal}'
exit 1
""",
    )
    operation_id = "00000000000000010000000000000001"

    controller.start(operation_id)

    assert _wait_terminal(controller, operation_id)["status"] == "failed"
    time.sleep(1.1)
    assert not descendant_marker.exists()


def test_failed_high_operation_id_does_not_poison_replay_watermark(tmp_path):
    controller = _controller(tmp_path, "exit 17")
    forged_operation_id = "ffffffffffffffff0000000000000001"
    legitimate_operation_id = "00000000000000010000000000000001"

    controller.start(forged_operation_id)
    assert _wait_terminal(controller, forged_operation_id)["status"] == "failed"

    controller.apply_program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    controller.apply_program.chmod(0o755)
    _, created = controller.start(legitimate_operation_id)

    assert created is True
    assert _wait_terminal(controller, legitimate_operation_id)["status"] == "succeeded"
    assert controller._read_state()["high_watermark"] == 1


def test_interrupted_operation_is_failed_on_restart(tmp_path):
    controller = _controller(tmp_path, "exit 0")
    state = controller._read_state()
    state["operations"]["5" * 32] = {
        "operation_id": "5" * 32,
        "status": "running",
    }
    controller._write_state(state)

    recovered = helper.OperationController(
        state_path=controller.state_path,
        request_path=controller.request_path,
        release_journal_path=controller.release_journal_path,
        log_path=controller.log_path,
        apply_program=controller.apply_program,
        project_dir=controller.project_dir,
    ).get("5" * 32)

    assert recovered["status"] == "failed"
    assert recovered["error"] == "ota_helper_restarted"
    assert recovered["log_end"] == 0


def test_interrupted_operation_with_durable_request_resumes_fixed_program(tmp_path):
    marker = tmp_path / "resumed"
    controller = _controller(tmp_path, f"printf x >> '{marker}'")
    operation_id = "6" * 32
    state = controller._read_state()
    state["operations"][operation_id] = {
        "operation_id": operation_id,
        "status": "running",
        "step": "apply",
        "log_start": 0,
        "log_available": True,
    }
    controller._write_state(state)
    controller._write_request(operation_id)

    resumed = helper.OperationController(
        state_path=controller.state_path,
        request_path=controller.request_path,
        release_journal_path=controller.release_journal_path,
        log_path=controller.log_path,
        apply_program=controller.apply_program,
        project_dir=controller.project_dir,
    )

    assert _wait_terminal(resumed, operation_id)["status"] == "succeeded"
    assert resumed._read_state()["high_watermark"] == helper.operation_sequence(operation_id)
    assert marker.read_text(encoding="utf-8") == "x"


def test_timed_out_program_is_terminated_and_releases_serial_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "APPLY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(helper, "PROCESS_TERMINATE_GRACE_SECONDS", 0.05)
    controller = _controller(tmp_path, "sleep 30")

    controller.start("7" * 32)
    timed_out = _wait_terminal(controller, "7" * 32)

    assert timed_out["status"] == "failed"
    assert timed_out["error"] == "fixed_state_machine_timeout"

    monkeypatch.setattr(helper, "APPLY_TIMEOUT_SECONDS", 1)
    controller.apply_program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    controller.apply_program.chmod(0o755)
    _, created = controller.start("8" * 32)
    assert created is True
    assert _wait_terminal(controller, "8" * 32)["status"] == "succeeded"


def test_evicted_operation_id_is_rejected_without_reexecution(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "MAX_FULL_OPERATIONS", 2)
    monkeypatch.setattr(helper, "MAX_OPERATION_TOMBSTONES", 3)
    marker = tmp_path / "calls"
    controller = _controller(tmp_path, f"printf x >> '{marker}'")

    for digit in "123456":
        operation_id = digit * 32
        controller.start(operation_id)
        _wait_terminal(controller, operation_id)

    state = controller._read_state()
    assert len(state["operations"]) == 2
    assert len(state["tombstones"]) == 3

    retained, created = controller.start("1" * 32)
    assert created is False
    assert retained["operation_id"] == "1" * 32
    assert retained["status"] == "failed"
    assert retained["error"] == "operation_replay_rejected"
    assert marker.read_text(encoding="utf-8") == "x" * 6


def test_finished_program_cannot_leave_log_pipe_open_forever(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "PROCESS_OUTPUT_DRAIN_GRACE_SECONDS", 0.05)
    controller = _controller(tmp_path, "sleep 30 & exit 0")

    controller.start("6" * 32)

    assert _wait_terminal(controller, "6" * 32)["status"] == "succeeded"


def test_process_output_and_shared_log_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "MAX_OPERATION_LOG_BYTES", 128)
    monkeypatch.setattr(helper, "LOG_FILE_MAX_BYTES", 1024)
    controller = _controller(tmp_path, "yes x | head -c 4096")

    for digit in "12345678":
        operation_id = digit * 32
        controller.start(operation_id)
        _wait_terminal(controller, operation_id)

    assert controller.log_path.stat().st_size <= helper.LOG_FILE_MAX_BYTES
    assert b"output truncated" in controller.log_path.read_bytes()


def test_log_rotation_persists_unavailable_state_before_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "MAX_OPERATION_LOG_BYTES", 8)
    monkeypatch.setattr(helper, "LOG_FILE_MAX_BYTES", 16)
    monkeypatch.setattr(helper, "LOG_ENTRY_OVERHEAD_BYTES", 0)
    controller = _controller(tmp_path, "exit 0")
    operation_id = "1" * 32
    state = controller._read_state()
    state["operations"][operation_id] = {
        "operation_id": operation_id,
        "status": "succeeded",
        "log_available": True,
        "log_start": 0,
        "log_end": 16,
    }
    controller._write_state(state)
    controller.log_path.write_bytes(b"x" * 16)

    controller._prepare_log_for_new_operation(state)

    persisted = controller._read_state()["operations"][operation_id]
    assert persisted["log_available"] is False
    assert persisted["log_start"] is None
    assert persisted["log_end"] is None
    assert controller.log_path.stat().st_size == 0


def test_log_rotation_failure_leaves_persisted_state_conservative(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "MAX_OPERATION_LOG_BYTES", 8)
    monkeypatch.setattr(helper, "LOG_FILE_MAX_BYTES", 16)
    monkeypatch.setattr(helper, "LOG_ENTRY_OVERHEAD_BYTES", 0)
    controller = _controller(tmp_path, "exit 0")
    operation_id = "1" * 32
    state = controller._read_state()
    state["operations"][operation_id] = {
        "operation_id": operation_id,
        "status": "succeeded",
        "log_available": True,
        "log_start": 0,
        "log_end": 16,
    }
    controller._write_state(state)
    controller.log_path.write_bytes(b"x" * 16)
    real_open = Path.open

    def fail_log_truncate(path, mode="r", *args, **kwargs):
        if path == controller.log_path and mode == "wb":
            raise OSError("simulated rotation crash")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_log_truncate)

    with pytest.raises(OSError, match="simulated rotation crash"):
        controller._prepare_log_for_new_operation(state)

    persisted = controller._read_state()["operations"][operation_id]
    assert persisted["log_available"] is False
    assert controller.log_path.stat().st_size == 16


def test_unix_http_api_accepts_only_operation_id(tmp_path):
    controller = _controller(tmp_path, "echo applied")
    socket_path = Path("/private/tmp") / f"dc-ota-{os.getpid()}-{time.time_ns()}.sock"
    try:
        server = helper.ThreadingUnixServer(str(socket_path), helper.OtaRequestHandler)
    except PermissionError:
        pytest.skip("sandbox forbids binding Unix sockets")
    server.controller = controller
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    operation_id = "6" * 32

    try:
        transport = httpx.HTTPTransport(uds=str(socket_path))
        with httpx.Client(transport=transport, base_url="http://helper.invalid") as client:
            rejected = client.post(
                "/ota/start",
                json={"operation_id": operation_id, "command": "id"},
            )
            started = client.post("/ota/start", json={"operation_id": operation_id})
            duplicate = client.post("/ota/start", json={"operation_id": operation_id})

            assert rejected.status_code == 422
            assert started.status_code == 202
            assert duplicate.status_code == 200

            _wait_terminal(controller, operation_id)
            status = client.get(f"/ota/operations/{operation_id}")
            logs = client.get(f"/ota/operations/{operation_id}/logs")
            escaped = client.get("/ota/operations/../ready")

            assert status.json()["status"] == "succeeded"
            assert "applied" in logs.json()["data"]
            assert escaped.status_code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        socket_path.unlink(missing_ok=True)
