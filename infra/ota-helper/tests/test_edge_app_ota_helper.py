from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


HELPER_PATH = Path(__file__).resolve().parents[1] / "edge_app_ota_helper.py"
if str(HELPER_PATH.parent) not in sys.path:
    sys.path.insert(0, str(HELPER_PATH.parent))
SPEC = importlib.util.spec_from_file_location("edge_app_ota_helper", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)
import edge_app_release as release  # noqa: E402


def _slot(release_id: str, sequence: int, suffix: str) -> dict:
    manifest = {
        "schema": "drycontroller.app-release.v2",
        "profile": "edge-app-v1",
        "release_id": release_id,
        "sequence": sequence,
        "source_commit": suffix * 40,
        "deployment_digest": "sha256:" + suffix * 64,
        "images": {
            "edge-api": f"registry.test/edge-api@sha256:{suffix * 64}",
            "edge-daq": f"registry.test/edge-daq@sha256:{suffix * 64}",
        },
    }
    return {
        "manifest": manifest,
        "manifest_digest": release.canonical_manifest_digest(manifest),
        "manifest_signature": "signed",
        "verified_at": "2026-08-30T00:00:00+00:00",
    }


@pytest.mark.parametrize("journal_phase", ["idle", "committed"])
def test_restart_after_durable_commit_marks_running_operation_succeeded_without_reapply(
    tmp_path: Path, monkeypatch, journal_phase: str
):
    operation_id = "00000000000000020000000000000002"
    active = _slot("22222222-2222-4222-8222-222222222222", 2, "2")
    previous = _slot("11111111-1111-4111-8111-111111111111", 1, "1")
    result = {
        "action": "apply",
        "operation_id": operation_id,
        "status": "committed",
        "release_id": active["manifest"]["release_id"],
        "manifest_digest": active["manifest_digest"],
        "sequence": 2,
        "previous_release_id": previous["manifest"]["release_id"],
    }
    state_path = tmp_path / "state/operations.json"
    request_path = tmp_path / "state/request.json"
    journal_path = tmp_path / "state/release-journal.json"
    log_path = tmp_path / "logs/operations.log"
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps(
            {
                "operations": {
                    operation_id: {
                        "operation_id": operation_id,
                        "status": "running",
                        "step": "apply",
                        "started_at": "2026-08-30T00:00:00+00:00",
                        "finished_at": None,
                        "exit_code": None,
                        "error": None,
                        "log_available": True,
                        "log_start": 0,
                        "log_end": None,
                    }
                },
                "tombstones": {},
                "high_watermark": -1,
            }
        )
    )
    request_path.write_text(json.dumps({"operation_id": operation_id}))
    journal_path.write_text(
        json.dumps(
            {
                "schema": "drycontroller.edge-app-release-journal.v2",
                "phase": journal_phase,
                "operation_id": operation_id if journal_phase == "committed" else None,
                "last_operation_id": operation_id,
                "last_result": result,
                "high_watermark": 2,
                "active": active,
                "previous": previous,
                "candidate": None,
                "error": None,
            }
        )
    )

    def unexpected_thread(*_args, **_kwargs):
        raise AssertionError("durably committed operation must not be replayed")

    monkeypatch.setattr(helper.threading, "Thread", unexpected_thread)
    controller = helper.OperationController(
        state_path=state_path,
        request_path=request_path,
        release_journal_path=journal_path,
        log_path=log_path,
        apply_program=tmp_path / "must-not-run",
        project_dir=tmp_path,
    )

    operation = controller.get(operation_id)
    assert operation["status"] == "succeeded"
    assert operation["exit_code"] == 0
    assert operation["release_id"] == active["manifest"]["release_id"]
    assert operation["manifest_digest"] == active["manifest_digest"]
    assert operation["sequence"] == 2
    assert "target_version" not in operation
    assert "active_release_version" not in operation
    assert json.loads(state_path.read_text())["high_watermark"] == 2
    assert json.loads(journal_path.read_text())["phase"] == journal_phase


def test_prune_preserves_signed_release_receipt(monkeypatch):
    operation_id = "00000000000000020000000000000002"
    release_id = "22222222-2222-4222-8222-222222222222"
    manifest_digest = "sha256:" + "2" * 64
    state = {
        "operations": {
            operation_id: {
                "operation_id": operation_id,
                "status": "succeeded",
                "step": "done",
                "started_at": "2026-08-30T00:00:00+00:00",
                "finished_at": "2026-08-30T00:01:00+00:00",
                "exit_code": 0,
                "error": None,
                "log_available": True,
                "release_id": release_id,
                "manifest_digest": manifest_digest,
                "sequence": 2,
            }
        },
        "tombstones": {},
        "high_watermark": 2,
    }
    controller = object.__new__(helper.OperationController)
    monkeypatch.setattr(helper, "MAX_FULL_OPERATIONS", 0)

    controller._prune_state(state)

    assert state["operations"] == {}
    assert state["tombstones"][operation_id]["release_id"] == release_id
    assert state["tombstones"][operation_id]["manifest_digest"] == manifest_digest
    assert state["tombstones"][operation_id]["sequence"] == 2


def test_release_summary_uses_signed_receipt_identity_without_version(tmp_path: Path):
    operation_id = "00000000000000020000000000000002"
    active = _slot("22222222-2222-4222-8222-222222222222", 2, "2")
    journal = {
        "schema": "drycontroller.edge-app-release-journal.v2",
        "phase": "idle",
        "operation_id": None,
        "last_operation_id": operation_id,
        "last_result": {
            "action": "apply",
            "operation_id": operation_id,
            "status": "committed",
            "release_id": active["manifest"]["release_id"],
            "manifest_digest": active["manifest_digest"],
            "sequence": 2,
            "previous_release_id": "11111111-1111-4111-8111-111111111111",
        },
        "high_watermark": 2,
        "active": active,
        "previous": None,
        "candidate": None,
        "error": None,
    }
    path = tmp_path / "release-journal.json"
    path.write_text(json.dumps(journal))
    controller = object.__new__(helper.OperationController)
    controller.release_journal_path = path

    assert controller._release_summary() == {
        "release_id": active["manifest"]["release_id"],
        "manifest_digest": active["manifest_digest"],
        "sequence": 2,
    }
