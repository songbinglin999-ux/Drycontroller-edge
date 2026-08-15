from __future__ import annotations

import importlib.util
import json
import os
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


def _physical_interface(tmp_path: Path, name: str = "enp2s0") -> Path:
    sys_class_net = tmp_path / "sys-class-net"
    interface = sys_class_net / name
    (interface / "device").mkdir(parents=True)
    (interface / "type").write_text("1\n", encoding="ascii")
    return sys_class_net


def _tailscale_lock(tmp_path: Path) -> Path:
    path = tmp_path / "tailscale-client.json"
    path.write_text(
        json.dumps(
            {
                "schema": "drycontroller.tailscale-client.v1",
                "version": "1.98.10",
                "architecture": "amd64",
                "deb_url": "https://pkgs.tailscale.com/stable/ubuntu/pool/tailscale_1.98.10_amd64.deb",
                "deb_sha256": "2d94d8e5adbb8bd49efbda9a51358bf8ea28be1bcfc55411c367be813e321907",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_public_config_generates_private_runtime_state_and_fixed_factory_channel(tmp_path):
    public = tmp_path / "public.json"
    public.write_text(
        json.dumps(
            {
                "schema": "drycontroller.os-installer-public-config.v2",
                "cloud_ops_base_url": "https://ops.example.test/",
                "tailscale_login_server": "https://headscale.example.test/",
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
    sys_class_net = _physical_interface(tmp_path)
    for target in (runtime, cloud, netplan):
        target.with_name(f".{target.name}.{os.getpid()}.tmp").write_bytes(b"interrupted")

    result = _module().initialize(
        public_config=public,
        tailscale_client_lock=_tailscale_lock(tmp_path),
        env_template=ROOT / "env.example",
        machine_id=machine_id,
        runtime_env=runtime,
        cloud_env=cloud,
        netplan=netplan,
        sys_class_net=sys_class_net,
    )
    first_runtime = runtime.read_bytes()
    second_result = _module().initialize(
        public_config=public,
        tailscale_client_lock=_tailscale_lock(tmp_path),
        env_template=ROOT / "env.example",
        machine_id=machine_id,
        runtime_env=runtime,
        cloud_env=cloud,
        netplan=netplan,
        sys_class_net=sys_class_net,
    )

    assert result["factory_interface"] == "enp2s0"
    assert second_result == result
    assert runtime.read_bytes() == first_runtime
    runtime_text = runtime.read_text(encoding="utf-8")
    assert "change_me_" not in runtime_text
    assert "LICENSE_MACHINE_ID=" + "a" * 32 in runtime_text
    assert "CLOUD_OPS_BASE_URL=https://ops.example.test" in runtime_text
    assert "TS_LOGIN_SERVER=https://headscale.example.test" in runtime_text
    assert "TAILSCALE_EXPECTED_VERSION=1.98.10" in runtime_text
    assert cloud.read_text(encoding="utf-8") == "CLOUD_OPS_BASE_URL=https://ops.example.test\n"
    netplan_text = netplan.read_text(encoding="utf-8")
    assert "    00-drycontroller-factory:\n" in netplan_text
    factory_definition = next(
        line.strip().removesuffix(":")
        for line in netplan_text.splitlines()
        if line.startswith("    ") and not line.startswith("      ") and line.endswith(":")
    )
    assert factory_definition < "all-en"
    assert factory_definition < "all-eth"
    assert "name: enp2s0" in netplan_text
    assert "addresses: [169.254.203.2/30]" in netplan_text
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o600
    assert stat.S_IMODE(cloud.stat().st_mode) == 0o600
    assert stat.S_IMODE(netplan.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda text: text.replace("LICENSE_MACHINE_ID=" + "a" * 32, "LICENSE_MACHINE_ID=" + "b" * 32), "machine-id"),
        (lambda text: text.replace("POSTGRES_PASSWORD=", "POSTGRES_PASSWORD=short-"), "secret value"),
        (lambda text: text + "UNEXPECTED=value\n", "fields"),
    ],
)
def test_existing_private_runtime_rejects_drift(tmp_path, mutation, error):
    public = tmp_path / "public.json"
    public.write_text(
        json.dumps(
            {
                "schema": "drycontroller.os-installer-public-config.v2",
                "cloud_ops_base_url": "https://ops.example.test",
                "tailscale_login_server": "https://headscale.example.test",
                "factory_interface": "enp2s0",
                "factory_address": "169.254.203.2/30",
            }
        ),
        encoding="utf-8",
    )
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("a" * 32, encoding="utf-8")
    runtime = tmp_path / "runtime.env"
    module = _module()
    arguments = {
        "public_config": public,
        "tailscale_client_lock": _tailscale_lock(tmp_path),
        "env_template": ROOT / "env.example",
        "machine_id": machine_id,
        "runtime_env": runtime,
        "cloud_env": tmp_path / "cloud.env",
        "netplan": tmp_path / "factory.yaml",
        "sys_class_net": _physical_interface(tmp_path),
    }
    module.initialize(**arguments)
    runtime.write_text(mutation(runtime.read_text(encoding="utf-8")), encoding="utf-8")
    runtime.chmod(0o600)

    with pytest.raises(ValueError, match=error):
        module.initialize(**arguments)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cloud_ops_base_url", "http://ops.example.test"),
        ("cloud_ops_base_url", "https://ops.example.test/?tenant=other"),
        ("cloud_ops_base_url", "https://ops.example.test/#fragment"),
        ("cloud_ops_base_url", "https://ops..example.test"),
        ("cloud_ops_base_url", "https://ops.example.test:99999"),
        ("cloud_ops_base_url", "https://ops.example.test:"),
        ("cloud_ops_base_url", "https://ops.example.test?"),
        ("cloud_ops_base_url", "https://ops.example.test#"),
        ("tailscale_login_server", "http://headscale.example.test"),
        ("tailscale_login_server", "https://headscale..example.test"),
        ("factory_interface", "enp2s0;bad"),
        ("factory_interface", "lo"),
        ("factory_interface", "docker0"),
        ("factory_interface", "veth1234"),
        ("factory_interface", "en012345678901234"),
        ("factory_address", "0.0.0.0/0"),
    ],
)
def test_public_config_rejects_noncanonical_or_exposed_channel(tmp_path, field, value):
    payload = {
        "schema": "drycontroller.os-installer-public-config.v2",
        "cloud_ops_base_url": "https://ops.example.test",
        "tailscale_login_server": "https://headscale.example.test",
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
            tailscale_client_lock=_tailscale_lock(tmp_path),
            env_template=ROOT / "env.example",
            machine_id=machine_id,
            runtime_env=tmp_path / "runtime.env",
            cloud_env=tmp_path / "cloud.env",
            netplan=tmp_path / "factory.yaml",
        )


@pytest.mark.parametrize("interface_state", ["missing", "virtual", "non-ethernet"])
def test_target_rejects_missing_or_virtual_factory_interface(tmp_path, interface_state):
    public = tmp_path / "public.json"
    public.write_text(
        json.dumps(
            {
                "schema": "drycontroller.os-installer-public-config.v2",
                "cloud_ops_base_url": "https://ops.example.test",
                "tailscale_login_server": "https://headscale.example.test",
                "factory_interface": "enp2s0",
                "factory_address": "169.254.203.2/30",
            }
        ),
        encoding="utf-8",
    )
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("a" * 32, encoding="utf-8")
    sys_class_net = tmp_path / "sys-class-net"
    if interface_state != "missing":
        interface = sys_class_net / "enp2s0"
        interface.mkdir(parents=True)
        (interface / "type").write_text(
            "1\n" if interface_state == "virtual" else "772\n",
            encoding="ascii",
        )
        if interface_state == "non-ethernet":
            (interface / "device").mkdir()

    with pytest.raises(ValueError, match="physical Ethernet"):
        _module().initialize(
            public_config=public,
            tailscale_client_lock=_tailscale_lock(tmp_path),
            env_template=ROOT / "env.example",
            machine_id=machine_id,
            runtime_env=tmp_path / "runtime.env",
            cloud_env=tmp_path / "cloud.env",
            netplan=tmp_path / "factory.yaml",
            sys_class_net=sys_class_net,
        )
