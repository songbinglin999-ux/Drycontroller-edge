from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from common.tailscale_status import inspect_tailscale_status, validate_tailscale_status


ROOT = Path(__file__).resolve().parents[1]
LOCK_MODULE = ROOT / "infra" / "bootstrap" / "tailscale_client_lock.py"
def _lock_module():
    spec = importlib.util.spec_from_file_location("tailscale_client_lock", LOCK_MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _headscale_lock(**candidate_overrides):
    candidate = {
        "version": "1.98.10",
        "deb_url": "https://pkgs.tailscale.com/stable/ubuntu/pool/tailscale_1.98.10_amd64.deb",
        "deb_sha256": "2d94d8e5adbb8bd49efbda9a51358bf8ea28be1bcfc55411c367be813e321907",
    }
    candidate.update(candidate_overrides)
    return {"tailscale_client": {"linux_amd64_candidate": candidate}}


def test_cloud_versions_lock_becomes_one_public_media_client_lock():
    result = _lock_module().from_headscale_versions_lock(_headscale_lock())

    assert result == {
        "schema": "drycontroller.tailscale-client.v1",
        "version": "1.98.10",
        "architecture": "amd64",
        "deb_url": "https://pkgs.tailscale.com/stable/ubuntu/pool/tailscale_1.98.10_amd64.deb",
        "deb_sha256": "2d94d8e5adbb8bd49efbda9a51358bf8ea28be1bcfc55411c367be813e321907",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": "stable"},
        {"deb_url": "https://example.test/tailscale.deb"},
        {"deb_sha256": "0" * 63},
    ],
)
def test_client_lock_rejects_mutable_or_unverifiable_package(overrides):
    with pytest.raises(ValueError):
        _lock_module().from_headscale_versions_lock(_headscale_lock(**overrides))


def test_actual_tailscale_status_matches_version_server_identity_and_online_state():
    responses = {
        ("status", "--json"): json.dumps({
            "Version": "1.98.10-tabcdef123-g123456789",
            "BackendState": "Running",
            "Self": {"Online": True, "ID": "node-7", "HostName": "edge-abcdef123456"},
        }),
        ("prefs", "--pretty"): 'Prefs{ra=false ssh=true url="https://headscale.example.test/" Persist{n=[redacted]}}',
    }

    def reader(arguments):
        return responses[tuple(arguments[-2:])]

    result = inspect_tailscale_status(
        expected_version="1.98.10",
        expected_login_server="https://headscale.example.test",
        expected_hostname="edge-abcdef123456",
        expected_node_id="node-7",
        command_reader=reader,
    )

    assert result["ok"] is True
    assert result["detail"]["version"] == "1.98.10"
    assert result["detail"]["version_long"] == "1.98.10-tabcdef123-g123456789"
    assert result["detail"]["node_id"] == "node-7"
    assert result["detail"]["ssh_enabled"] is True
    assert "redacted" not in json.dumps(result)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("Version", "1.98.9", "tailscale_version_mismatch"),
        ("BackendState", "NeedsLogin", "tailscale_not_running"),
        ("Online", False, "tailscale_self_offline"),
        ("ID", "", "tailscale_node_identity_missing"),
        ("HostName", "edge-node", "tailscale_hostname_mismatch"),
        ("ControlURL", "https://login.tailscale.com", "tailscale_login_server_mismatch"),
        ("RunSSH", False, "tailscale_ssh_disabled"),
    ],
)
def test_actual_tailscale_status_fails_closed_on_runtime_drift(field, value, reason):
    status = {
        "Version": "1.98.10",
        "BackendState": "Running",
        "Self": {"Online": True, "ID": "node-7", "HostName": "edge-abcdef123456"},
    }
    control_url = "https://headscale.example.test"
    if field in status:
        status[field] = value
    elif field in status["Self"]:
        status["Self"][field] = value
    elif field == "ControlURL":
        control_url = value

    def reader(arguments):
        if arguments[-2:] == ["status", "--json"]:
            return json.dumps(status)
        ssh = "true" if field != "RunSSH" or value else "false"
        return f'Prefs{{ra=false ssh={ssh} url="{control_url}" Persist{{n=[redacted]}}}}'

    result = inspect_tailscale_status(
        expected_version="1.98.10",
        expected_login_server="https://headscale.example.test",
        expected_hostname="edge-abcdef123456",
        expected_node_id="node-7",
        command_reader=reader,
    )

    assert result["ok"] is False
    assert result["reason"] == reason


def test_actual_tailscale_status_rejects_replacement_node_identity():
    def reader(arguments):
        if arguments[-2:] == ["status", "--json"]:
            return json.dumps(
                {
                    "Version": "1.98.10",
                    "BackendState": "Running",
                    "Self": {
                        "Online": True,
                        "ID": "replacement-node",
                        "HostName": "edge-abcdef123456",
                    },
                }
            )
        return 'Prefs{ssh=true url="https://headscale.example.test"}'

    result = inspect_tailscale_status(
        expected_version="1.98.10",
        expected_login_server="https://headscale.example.test",
        expected_hostname="edge-abcdef123456",
        expected_node_id="node-7",
        command_reader=reader,
    )

    assert result["ok"] is False
    assert result["reason"] == "tailscale_node_identity_mismatch"


def test_tailnet_product_ingress_is_interface_bound_and_preflight_gated():
    socket_unit = (ROOT / "infra/bootstrap/drycontroller-tailnet-edge-api.socket").read_text()
    service_unit = (ROOT / "infra/bootstrap/drycontroller-tailnet-edge-api.service").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "ListenStream=0.0.0.0:8000" in socket_unit
    assert "BindToDevice=tailscale0" in socket_unit
    assert "/usr/local/libexec/drycontroller/tailnet-edge-api-proxy.py" in service_unit
    assert "systemd-socket-proxyd" not in service_unit
    assert '"127.0.0.1:${EDGE_API_LOCAL_PORT:-18080}:8000"' in compose
    assert "EDGE_API_BIND_ADDR" not in compose
    observation = {
        "ok": True,
        "reason": "observed",
        "detail": {
            "version": "1.98.10",
            "backend_state": "Running",
            "self_online": True,
            "node_id": "node-7",
            "hostname": "edge-abcdef123456",
            "login_server": "https://headscale.example.test",
            "ssh_enabled": True,
            "edge_api_ingress_active": False,
        },
    }
    result = validate_tailscale_status(
        observation,
        expected_version="1.98.10",
        expected_login_server="https://headscale.example.test",
        expected_hostname="edge-abcdef123456",
        expected_node_id="node-7",
        require_edge_api_ingress=True,
    )
    assert result["ok"] is False
    assert result["reason"] == "tailnet_edge_api_ingress_inactive"
