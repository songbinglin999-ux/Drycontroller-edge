from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "host_release_runner.py"
SPEC = importlib.util.spec_from_file_location("host_release_runner", MODULE_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def _idle_journal() -> dict:
    return {
        "schema": runner.COORDINATOR_JOURNAL_SCHEMA,
        "phase": "idle",
        "operation_id": None,
        "last_operation_id": None,
        "high_watermark": -1,
        "active": None,
        "candidate": None,
        "previous": None,
        "effect": None,
        "error": None,
    }


def _controller(tmp_path: Path, script: str) -> runner.OperationController:
    apply_program = tmp_path / "host-release-apply"
    apply_program.write_text(f"#!/bin/sh\nset -eu\n{script}\n", encoding="utf-8")
    apply_program.chmod(0o755)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "coordinator-journal.json").write_text(json.dumps(_idle_journal()), encoding="utf-8")
    return runner.OperationController(
        state_path=state_dir / "operations.json",
        request_path=state_dir / "request.json",
        coordinator_journal_path=state_dir / "coordinator-journal.json",
        log_path=tmp_path / "log" / "operations.log",
        apply_program=apply_program,
    )


def _wait_terminal(controller: runner.OperationController, operation_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        operation = controller.get(operation_id)
        if operation["status"] in runner.TERMINAL:
            return operation
        time.sleep(0.01)
    raise AssertionError("HostRelease operation did not finish")


def _wait_recovery_pending(controller: runner.OperationController, operation_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        operation = controller.get(operation_id)
        if operation.get("error") == "host_release_recovery_pending":
            return operation
        time.sleep(0.01)
    raise AssertionError("HostRelease operation did not retain recovery-pending state")


@pytest.mark.parametrize("value", ["", "0" * 31, "0" * 33, "A" * 32, "../" + "0" * 29])
def test_operation_id_is_exact_lowercase_hex(value):
    with pytest.raises(runner.InvalidOperation):
        runner.validate_operation_id(value)


def test_request_accepts_only_operation_id():
    operation_id = "a" * 32
    assert runner.operation_id_from_body({"operation_id": operation_id}) == operation_id
    with pytest.raises(runner.InvalidOperation):
        runner.operation_id_from_body({"operation_id": operation_id, "command": "id"})


def test_same_operation_is_idempotent_and_apply_receives_no_arguments(tmp_path):
    marker = tmp_path / "calls"
    controller = _controller(tmp_path, f"test \"$#\" -eq 0; printf x >> '{marker}'")
    operation_id = "1" * 32

    first = controller.start(operation_id)
    duplicate = controller.start(operation_id)
    finished = _wait_terminal(controller, operation_id)

    assert first["operation_id"] == duplicate["operation_id"] == operation_id
    assert finished["status"] == "succeeded"
    assert marker.read_text(encoding="utf-8") == "x"
    assert json.loads(controller.request_path.read_text(encoding="utf-8")) == {"operation_id": operation_id}


@pytest.mark.parametrize(
    "interruption",
    ["kill -KILL $$", "sleep 30"],
)
def test_child_interruption_recovers_non_idle_coordinator_before_terminal_state(
    tmp_path, monkeypatch, interruption
):
    monkeypatch.setattr(runner, "APPLY_TIMEOUT_SECONDS", 5)
    marker = tmp_path / "calls"
    journal = tmp_path / "state" / "coordinator-journal.json"
    controller = _controller(
        tmp_path,
        f"""
if [ ! -f '{marker}' ]; then
    printf x > '{marker}'
    printf '%s' '{{"phase":"candidate_activating"}}' > '{journal}'
    {interruption}
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

    duplicate = controller.start(operation_id)
    restarted = runner.OperationController(
        state_path=controller.state_path,
        request_path=controller.request_path,
        coordinator_journal_path=controller.coordinator_journal_path,
        log_path=controller.log_path,
        apply_program=controller.apply_program,
    ).get(operation_id)

    assert duplicate["status"] == restarted["status"] == "failed"
    assert marker.read_text(encoding="utf-8") == "xx"
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "idle"


def test_unfinished_recovery_blocks_new_id_and_same_id_can_resume(tmp_path):
    journal = tmp_path / "state" / "coordinator-journal.json"
    controller = _controller(
        tmp_path,
        f"printf '%s' '{{\"phase\":\"candidate_activating\"}}' > '{journal}'; exit 1",
    )
    operation_id = "00000000000000010000000000000001"
    controller.start(operation_id)

    assert _wait_recovery_pending(controller, operation_id)["status"] == "queued"
    with pytest.raises(runner.OperationBusy) as raised:
        controller.start("00000000000000020000000000000001")
    assert str(raised.value) == operation_id

    controller.apply_program.write_text(
        f"#!/bin/sh\nprintf '%s' '{json.dumps(_idle_journal())}' > '{journal}'\nexit 1\n",
        encoding="utf-8",
    )
    controller.apply_program.chmod(0o755)
    duplicate = controller.start(operation_id)

    assert duplicate["status"] == "queued"
    assert _wait_terminal(controller, operation_id)["status"] == "failed"
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "idle"


@pytest.mark.parametrize("journal_body", ["not-json", '{"phase":42}', '{"phase":"idle"}'])
def test_invalid_coordinator_journal_stays_recovery_pending(tmp_path, journal_body):
    journal = tmp_path / "state" / "coordinator-journal.json"
    controller = _controller(tmp_path, f"printf '%s' '{journal_body}' > '{journal}'; exit 1")
    operation_id = "00000000000000010000000000000001"

    controller.start(operation_id)

    assert _wait_recovery_pending(controller, operation_id)["status"] == "queued"
    with pytest.raises(runner.OperationBusy):
        controller.start("00000000000000020000000000000001")


def test_child_sigkill_terminates_descendant_before_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "PROCESS_TERMINATE_GRACE_SECONDS", 0.05)
    marker = tmp_path / "calls"
    descendant_marker = tmp_path / "descendant"
    journal = tmp_path / "state" / "coordinator-journal.json"
    controller = _controller(
        tmp_path,
        f"""
if [ ! -f '{marker}' ]; then
    printf x > '{marker}'
    printf '%s' '{{"phase":"candidate_activating"}}' > '{journal}'
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


def test_different_operation_is_rejected_while_active(tmp_path):
    controller = _controller(tmp_path, "sleep 0.2")
    controller.start("2" * 32)

    with pytest.raises(runner.OperationBusy):
        controller.start("3" * 32)

    _wait_terminal(controller, "2" * 32)


def test_restart_resumes_only_matching_durable_request(tmp_path):
    marker = tmp_path / "resumed"
    controller = _controller(tmp_path, f"printf x >> '{marker}'")
    operation_id = "4" * 32
    state = controller._read_state()
    state["operations"][operation_id] = {
        "operation_id": operation_id,
        "status": "running",
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "error": None,
        "log_start": 0,
        "log_end": None,
    }
    controller._write_state(state)
    controller._write_request(operation_id)

    recovered = runner.OperationController(
        state_path=controller.state_path,
        request_path=controller.request_path,
        coordinator_journal_path=controller.coordinator_journal_path,
        log_path=controller.log_path,
        apply_program=controller.apply_program,
    )

    assert _wait_terminal(recovered, operation_id)["status"] == "succeeded"
    assert marker.read_text(encoding="utf-8") == "x"


def test_restart_without_matching_request_fails_closed(tmp_path):
    controller = _controller(tmp_path, "exit 0")
    operation_id = "5" * 32
    state = controller._read_state()
    state["operations"][operation_id] = {"operation_id": operation_id, "status": "running"}
    controller._write_state(state)

    recovered = runner.OperationController(
        state_path=controller.state_path,
        request_path=controller.request_path,
        coordinator_journal_path=controller.coordinator_journal_path,
        log_path=controller.log_path,
        apply_program=controller.apply_program,
    ).get(operation_id)

    assert recovered["status"] == "failed"
    assert recovered["error"] == "host_release_runner_restarted_without_request"
