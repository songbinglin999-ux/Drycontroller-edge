from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "bootstrap" / "initialize-runtime-config.py"


def _module():
    spec = importlib.util.spec_from_file_location("initialize_runtime_config", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_config_generates_private_runtime_state_and_fixed_factory_channel(tmp_path):
    public = tmp_path / "public.json"
    public.write_text(
        json.dumps(
            {
                "schema": "drycontroller.os-installer-public-config.v1",
                "cloud_ops_base_url": "https://ops.example.test/",
                "factory_interface": "enp2s0",
                "factory_address": "169.254.203.2/30",
            }
        ),
        encoding="utf-8",
    )
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("a" * 32 + "\n", encoding="utf-8")
    runtime = tmp_path / "runtime.env"
    cloud = tmp_path / "cloud.env"
    netplan = tmp_path / "factory.yaml"

    result = _module().initialize(
        public_config=public,
        env_template=ROOT / "env.example",
        machine_id=machine_id,
        runtime_env=runtime,
        cloud_env=cloud,
        netplan=netplan,
    )

    assert result["factory_interface"] == "enp2s0"
    runtime_text = runtime.read_text(encoding="utf-8")
    assert "change_me_" not in runtime_text
    assert "LICENSE_MACHINE_ID=" + "a" * 32 in runtime_text
    assert "CLOUD_OPS_BASE_URL=https://ops.example.test" in runtime_text
    assert cloud.read_text(encoding="utf-8") == "CLOUD_OPS_BASE_URL=https://ops.example.test\n"
    assert "name: enp2s0" in netplan.read_text(encoding="utf-8")
    assert "addresses: [169.254.203.2/30]" in netplan.read_text(encoding="utf-8")
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o600
    assert stat.S_IMODE(cloud.stat().st_mode) == 0o600
    assert stat.S_IMODE(netplan.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cloud_ops_base_url", "http://ops.example.test"),
        ("factory_interface", "enp2s0;bad"),
        ("factory_address", "0.0.0.0/0"),
    ],
)
def test_public_config_rejects_noncanonical_or_exposed_channel(tmp_path, field, value):
    payload = {
        "schema": "drycontroller.os-installer-public-config.v1",
        "cloud_ops_base_url": "https://ops.example.test",
        "factory_interface": "enp2s0",
        "factory_address": "169.254.203.2/30",
    }
    payload[field] = value
    public = tmp_path / "public.json"
    public.write_text(json.dumps(payload), encoding="utf-8")
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("a" * 32, encoding="utf-8")

    with pytest.raises(ValueError):
        _module().initialize(
            public_config=public,
            env_template=ROOT / "env.example",
            machine_id=machine_id,
            runtime_env=tmp_path / "runtime.env",
            cloud_env=tmp_path / "cloud.env",
            netplan=tmp_path / "factory.yaml",
        )
