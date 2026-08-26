from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scripts/ci/formal-modbus-fixture.py"


def _load_fixture():
    spec = importlib.util.spec_from_file_location("formal_modbus_fixture", FIXTURE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_formal_modbus_fixture_defines_45_nontrivial_expected_points() -> None:
    expected = _load_fixture().expected_tags()

    assert len(expected) == 45
    assert set(point["quality"] for point in expected.values()) == {"good"}
    assert expected["M1_I"]["value"] == 10.1
    assert expected["FTT1_T"]["value"] == 40.1
    assert expected["MT1_H"]["value"] == 32
    assert expected["PLC_LEGACY_HEARTBEAT_TOGGLE"]["must_transition"] is True
    assert sum(point.get("value") not in (None, 0, False) for point in expected.values()) >= 35


def test_formal_modbus_expectation_cli_is_canonical_json() -> None:
    completed = subprocess.run(
        [sys.executable, str(FIXTURE), "--print-expectations"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    payload = json.loads(completed.stdout)
    assert payload == _load_fixture().expected_tags()
    assert completed.stdout.strip() == json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )
