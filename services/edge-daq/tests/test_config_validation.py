import importlib
from pathlib import Path

import pytest


def test_config_validation_semantic_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))
    daq_main = importlib.import_module("daq.main")
    svc = daq_main.DAQService()

    bad_config = {
        "tags": {
            "s": {
                "c": [
                    {"name": "A", "type": "int", "signal_type": "BAD"},
                    {"name": "", "type": ""},  # missing
                    {"name": "C", "type": "int", "range": [1]},  # bad range
                ]
            }
        }
    }

    errors = svc._validate_config(bad_config)
    assert len(errors) >= 3
    assert any("invalid_signal_type" in e for e in errors)
    assert any("missing_field" in e for e in errors)

    good_config = {
        "tags": {
            "detection": {
                "AI": [
                    {"name": "FTT1", "type": "float", "signal_type": "AI"},
                ]
            }
        }
    }
    assert svc._validate_config(good_config) == []


@pytest.mark.asyncio
async def test_real_config_authority_diagnostics_validate(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("TAGS_CONFIG", str(repo_root / "configs/tags.yaml"))
    monkeypatch.setenv("MODBUS_ADDRESS_MAP", str(repo_root / "configs/modbus-address-map.yaml"))
    daq_main = importlib.import_module("daq.main")
    svc = daq_main.DAQService()

    config = await svc.load_config()

    assert svc._validate_config(config) == []
    authority_tags = config["tags"]["diagnostic"]["authority"]
    assert {tag["signal_type"] for tag in authority_tags} == {"DIAG"}


@pytest.mark.asyncio
async def test_real_readonly_point_table_is_28_words_plus_6_coils(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("TAGS_CONFIG", str(repo_root / "configs/tags.yaml"))
    monkeypatch.setenv("MODBUS_ADDRESS_MAP", str(repo_root / "configs/modbus-address-map.yaml"))
    daq_main = importlib.import_module("daq.main")
    config = await daq_main.DAQService().load_config()

    word_tags = [
        tag
        for category in config["tags"]["process"].values()
        for tag in category
    ]
    coil_tags = config["tags"]["detection"]["limit_switch"]

    assert len(word_tags) == 28
    assert len(coil_tags) == 6
    assert {tag["function_code"] for tag in word_tags} == {3}
    assert {tag["function_code"] for tag in coil_tags} == {1}
    assert not ({tag["address"] for tag in word_tags} & set(range(40030, 40036)))
