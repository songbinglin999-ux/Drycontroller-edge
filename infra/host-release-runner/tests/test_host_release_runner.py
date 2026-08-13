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


def _controller(tmp_path: Path, script: str) -> runner.OperationController:
    apply_program = tmp_path / "host-release-apply"
    apply_program.write_text(f"#!/bin/sh\nset -eu\n{script}\n", encoding="utf-8")
    apply_program.chmod(0o755)
    return runner.OperationController(
        state_path=tmp_path / "state" / "operations.json",
        request_path=tmp_path / "state" / "request.json",
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
        log_path=controller.log_path,
        apply_program=controller.apply_program,
    ).get(operation_id)

    assert recovered["status"] == "failed"
    assert recovered["error"] == "host_release_runner_restarted_without_request"
