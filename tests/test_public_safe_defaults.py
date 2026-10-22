"""Guards for the safe defaults of the public release."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_example_hmi_proxy_config_keeps_writes_disabled():
    config = yaml.safe_load((ROOT / "configs/modbus-proxy.yaml").read_text(encoding="utf-8"))
    assert config["write_enabled"] is False
    assert config["source_network_verified"] is False
    assert config["write_rules"] == []
    for item in config["write_ranges"]:
        assert item["end_address"] - item["start_address"] < 16
