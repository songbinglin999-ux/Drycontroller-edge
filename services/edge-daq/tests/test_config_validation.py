import importlib


def test_config_validation_duplicate_and_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))
    daq_main = importlib.import_module("daq.main")
    svc = daq_main.DAQService()

    bad_config = {
        "tags": {
            "s": {
                "c": [
                    {"name": "A", "address": 1, "type": "int"},
                    {"name": "B", "address": 1, "type": "int"},  # duplicate
                    {"name": "", "address": None, "type": ""},   # missing
                    {"name": "C", "address": 2, "type": "int", "range": [1]},  # bad range
                ]
            }
        }
    }

    errors = svc._validate_config(bad_config)
    assert len(errors) >= 3
    assert any("duplicate_address" in e for e in errors)
    assert any("missing_field" in e for e in errors)

