from __future__ import annotations

import importlib.util
import json
import os
import threading
import time
from pathlib import Path

import httpx
import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "drycontroller_ota_helper.py"
SPEC = importlib.util.spec_from_file_location("drycontroller_ota_helper", MODULE_PATH)
assert SPEC and SPEC.loader
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def _controller(tmp_path: Path, script: str) -> helper.OperationController:
    apply_program = tmp_path / "apply"
    apply_program.write_text(f"#!/bin/sh\nset -eu\n{script}\n", encoding="utf-8")
    apply_program.chmod(0o755)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    return helper.OperationController(
        state_path=tmp_path / "state" / "operations.json",
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
    assert list((tmp_path / "state").iterdir()) == [tmp_path / "state" / "operations.json"]
    state = json.loads((tmp_path / "state" / "operations.json").read_text(encoding="utf-8"))
    assert set(state["operations"]) == {operation_id}


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
        log_path=controller.log_path,
        apply_program=controller.apply_program,
        project_dir=controller.project_dir,
    ).get("5" * 32)

    assert recovered["status"] == "failed"
    assert recovered["error"] == "ota_helper_restarted"
    assert recovered["log_end"] == 0


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
