"""Read and sanitize the actual local tailscaled status for preflight."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Callable


TAILSCALE_BIN = "/usr/bin/tailscale"
TAILSCALE_SOCKET = "/var/run/tailscale/tailscaled.sock"
MAX_OUTPUT_BYTES = 1024 * 1024
LONG_VERSION = re.compile(r"^([0-9]+\.[0-9]+\.[0-9]+)(?:-|$)")
CommandReader = Callable[[list[str]], str]


def _read_command(arguments: list[str]) -> str:
    completed = subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env={"HOME": "/tmp", "PATH": "/usr/bin:/bin"},
    )
    if completed.returncode != 0:
        raise ValueError("tailscale_command_failed")
    if len(completed.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ValueError("tailscale_output_too_large")
    return completed.stdout


def read_tailscale_status(
    *,
    tailscale_bin: str = TAILSCALE_BIN,
    socket_path: str = TAILSCALE_SOCKET,
    command_reader: CommandReader = _read_command,
) -> dict[str, object]:
    detail: dict[str, object] = {}
    try:
        status_raw = command_reader(
            [tailscale_bin, f"--socket={socket_path}", "status", "--json"]
        )
        status = json.loads(status_raw)
        if not isinstance(status, dict):
            raise ValueError("tailscale_output_invalid")
        # --pretty intentionally avoids fetching the raw Persist JSON, which
        # contains node identity material. Only its public url= summary is read.
        preferences = command_reader(
            [tailscale_bin, f"--socket={socket_path}", "debug", "prefs", "--pretty"]
        )
        control_url_match = re.search(r'(?:^|[ {])url="(https://[^"\\]+)"(?:[ }]|$)', preferences)
        control_url = control_url_match.group(1) if control_url_match else None
    except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else "tailscale_command_failed"
        return {"ok": False, "reason": reason, "detail": detail}

    self_status = status.get("Self")
    if not isinstance(self_status, dict):
        return {"ok": False, "reason": "tailscale_self_missing", "detail": detail}
    version_long = status.get("Version")
    version_match = LONG_VERSION.match(version_long) if isinstance(version_long, str) else None
    detail.update(
        {
            "version": version_match.group(1) if version_match else None,
            "version_long": version_long,
            "backend_state": status.get("BackendState"),
            "self_online": self_status.get("Online"),
            "node_id": self_status.get("ID"),
            "hostname": self_status.get("HostName"),
            "login_server": control_url,
        }
    )
    return {"ok": True, "reason": "observed", "detail": detail}


def validate_tailscale_status(
    observation: dict[str, object],
    *,
    expected_version: str,
    expected_login_server: str,
    expected_hostname: str,
) -> dict[str, object]:
    observed_detail = observation.get("detail")
    detail: dict[str, object] = {
        "expected_version": expected_version,
        "expected_login_server": expected_login_server.rstrip("/"),
        "expected_hostname": expected_hostname,
    }
    if isinstance(observed_detail, dict):
        detail.update(observed_detail)
    if observation.get("ok") is not True:
        reason = observation.get("reason")
        return {
            "ok": False,
            "reason": reason if isinstance(reason, str) else "tailscale_observation_invalid",
            "detail": detail,
        }
    if not expected_version or not expected_login_server or not expected_hostname:
        return {"ok": False, "reason": "tailnet_expectation_missing", "detail": detail}
    checks = (
        (detail.get("version") == expected_version, "tailscale_version_mismatch"),
        (detail.get("backend_state") == "Running", "tailscale_not_running"),
        (detail.get("self_online") is True, "tailscale_self_offline"),
        (
            isinstance(detail.get("node_id"), str) and bool(detail.get("node_id")),
            "tailscale_node_identity_missing",
        ),
        (detail.get("hostname") == expected_hostname, "tailscale_hostname_mismatch"),
        (
            isinstance(detail.get("login_server"), str)
            and str(detail["login_server"]).rstrip("/") == expected_login_server.rstrip("/"),
            "tailscale_login_server_mismatch",
        ),
    )
    for passed, reason in checks:
        if not passed:
            return {"ok": False, "reason": reason, "detail": detail}
    return {"ok": True, "reason": "ok", "detail": detail}


def inspect_tailscale_status(
    *,
    expected_version: str,
    expected_login_server: str,
    expected_hostname: str,
    tailscale_bin: str = TAILSCALE_BIN,
    socket_path: str = TAILSCALE_SOCKET,
    command_reader: CommandReader = _read_command,
) -> dict[str, object]:
    observation = read_tailscale_status(
        tailscale_bin=tailscale_bin,
        socket_path=socket_path,
        command_reader=command_reader,
    )
    return validate_tailscale_status(
        observation,
        expected_version=expected_version,
        expected_login_server=expected_login_server,
        expected_hostname=expected_hostname,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--login-server", required=True)
    parser.add_argument("--identity-file", required=True, type=Path)
    parser.add_argument("--tailscale-bin", default=TAILSCALE_BIN)
    parser.add_argument("--socket", default=TAILSCALE_SOCKET)
    args = parser.parse_args()
    try:
        identity = json.loads(args.identity_file.read_text(encoding="utf-8"))
        hostname = identity["node_name"]
        if not isinstance(hostname, str) or not hostname:
            raise ValueError("device identity node_name invalid")
        result = inspect_tailscale_status(
            expected_version=args.expected_version,
            expected_login_server=args.login_server,
            expected_hostname=hostname,
            tailscale_bin=args.tailscale_bin,
            socket_path=args.socket,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError) as exc:
        result = {"ok": False, "reason": "device_identity_invalid", "detail": {"error": str(exc)}}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
