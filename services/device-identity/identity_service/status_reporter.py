from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Callable
import urllib.error
import urllib.request

from .contract import parse_timestamp
from .state import IdentityStateStore


class StatusReportError(RuntimeError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class UrlLibStatusCloudClient:
    def __init__(self, base_url: str):
        if not base_url.startswith("https://"):
            raise StatusReportError("Cloud status URL must use HTTPS")
        self.base_url = base_url.rstrip("/")
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def report(self, payload: dict[str, Any], *, credential: str) -> dict[str, Any]:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/edge-status",
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Device-Credential": credential,
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=15) as response:
                raw = response.read(65_537)
        except (OSError, urllib.error.URLError) as exc:
            raise StatusReportError("Cloud status endpoint is unavailable") from exc
        if len(raw) > 65_536:
            raise StatusReportError("Cloud status response is too large")
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StatusReportError("Cloud status response is invalid") from exc
        if not isinstance(result, dict) or result.get("success") is not True:
            raise StatusReportError("Cloud status report was rejected")
        return result


def _run_command(args: list[str]) -> str:
    try:
        completed = subprocess.run(
            args,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StatusReportError(f"status command failed: {args[0]}") from exc
    if completed.returncode != 0 or len(completed.stdout) > 256 * 1024:
        raise StatusReportError(f"status command failed: {args[0]}")
    return completed.stdout


def _cpu_times(path: Path = Path("/proc/stat")) -> tuple[int, int]:
    try:
        fields = path.read_text(encoding="ascii").splitlines()[0].split()
        values = [int(value) for value in fields[1:]]
    except (OSError, UnicodeDecodeError, IndexError, ValueError) as exc:
        raise StatusReportError("host CPU status is unavailable") from exc
    if fields[0] != "cpu" or len(values) < 4:
        raise StatusReportError("host CPU status is invalid")
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def _cpu_percent(*, sleep: Callable[[float], None] = time.sleep) -> float:
    total_before, idle_before = _cpu_times()
    # The minute report should average short bursts instead of retaining a 100 ms spike.
    sleep(5.0)
    total_after, idle_after = _cpu_times()
    total_delta = total_after - total_before
    idle_delta = idle_after - idle_before
    if total_delta <= 0 or idle_delta < 0:
        raise StatusReportError("host CPU sample is invalid")
    return round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 1)


def _memory_percent(path: Path = Path("/proc/meminfo")) -> float:
    try:
        values = {
            key.rstrip(":"): int(value)
            for key, value, *_rest in (line.split() for line in path.read_text(encoding="ascii").splitlines())
        }
        total = values["MemTotal"]
        available = values["MemAvailable"]
    except (OSError, UnicodeDecodeError, KeyError, ValueError) as exc:
        raise StatusReportError("host memory status is unavailable") from exc
    if total <= 0 or not 0 <= available <= total:
        raise StatusReportError("host memory status is invalid")
    return round((total - available) / total * 100, 1)


def _disk_percent(path: Path = Path("/")) -> float:
    try:
        usage = os.statvfs(path)
    except OSError as exc:
        raise StatusReportError("host disk status is unavailable") from exc
    total = usage.f_blocks * usage.f_frsize
    available = usage.f_bavail * usage.f_frsize
    if total <= 0 or not 0 <= available <= total:
        raise StatusReportError("host disk status is invalid")
    return round((total - available) / total * 100, 1)


def _uptime(path: Path = Path("/proc/uptime")) -> int:
    try:
        value = float(path.read_text(encoding="ascii").split()[0])
    except (OSError, UnicodeDecodeError, IndexError, ValueError) as exc:
        raise StatusReportError("host uptime is unavailable") from exc
    if not math.isfinite(value) or value < 0:
        raise StatusReportError("host uptime is invalid")
    return int(value)


def _docker_services(run_command: Callable[[list[str]], str] = _run_command) -> list[dict[str, str]]:
    raw = run_command(["docker", "ps", "-a", "--format", "{{json .}}"])
    services: list[dict[str, str]] = []
    infrastructure_names = {
        "drycontroller-ipc-influxdb",
        "drycontroller-ipc-mosquitto",
        "drycontroller-ipc-plc-simulator",
        "drycontroller-ipc-postgres",
    }
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StatusReportError("Docker status output is invalid") from exc
        name = item.get("Names")
        state = str(item.get("State") or "").lower()
        detail = str(item.get("Status") or "")
        labels = str(item.get("Labels") or "")
        if not isinstance(name, str) or not name or len(name) > 128:
            raise StatusReportError("Docker status output is invalid")
        label_set = set(labels.split(","))
        is_edge_app = "com.docker.compose.project=drycontroller-edge" in label_set
        is_edge_infrastructure = (
            "com.docker.compose.project=drycontroller-ipc-mainline" in label_set
            and name in infrastructure_names
        )
        if not is_edge_app and not is_edge_infrastructure:
            continue
        if state == "running" and "(unhealthy)" in detail.lower():
            status = "unhealthy"
        elif state == "running":
            status = "running"
        elif state in {"exited", "dead"}:
            status = "down"
        else:
            status = state or "unknown"
        services.append({"name": name, "status": status, "detail": detail[:256]})
    return sorted(services, key=lambda item: item["name"])


def _tailnet_ipv4(run_command: Callable[[list[str]], str] = _run_command) -> str:
    for raw in run_command(["tailscale", "ip", "-4"]).splitlines():
        try:
            address = ipaddress.ip_address(raw.strip())
        except ValueError:
            continue
        if address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10"):
            return str(address)
    raise StatusReportError("Edge Tailnet IPv4 address is unavailable")


def build_status_payload(*, now: dt.datetime) -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "system": {
                "metrics": {
                    "cpu_percent": _cpu_percent(),
                    "memory_percent": _memory_percent(),
                    "disk_percent": _disk_percent(),
                    "uptime_sec": _uptime(),
                    "updated_at": int(now.timestamp()),
                }
            },
            "services": _docker_services(),
        },
        "timestamp": int(now.timestamp()),
    }


def _active_identity(store: IdentityStateStore, *, now: dt.datetime) -> dict[str, Any]:
    active = store.active()
    required = {
        "device_id",
        "tenant_id",
        "line_id",
        "node_name",
        "identity_fingerprint",
        "runtime_credential",
        "runtime_credential_hash",
        "credential_expires_at",
    }
    if (
        not isinstance(active, dict)
        or active.get("schema") != "drycontroller.device-identity.v1"
        or any(not isinstance(active.get(field), str) or not active[field] for field in required)
        or active["node_name"] != active["device_id"].lower()
        or hashlib.sha256(active["runtime_credential"].encode("utf-8")).hexdigest()
        != active["runtime_credential_hash"]
    ):
        raise StatusReportError("active device identity is unavailable")
    try:
        expires_at = parse_timestamp(active["credential_expires_at"])
    except (TypeError, ValueError) as exc:
        raise StatusReportError("active device identity is unavailable") from exc
    if expires_at <= now:
        raise StatusReportError("active device credential is expired")
    return active


def report_from_environment(*, verify_callback: bool = False) -> dict[str, Any]:
    if os.geteuid() != 0 and os.getenv("DRYCONTROLLER_IDENTITY_ALLOW_NON_ROOT_TEST") != "1":
        raise StatusReportError("Edge status reporting must run as root")
    store = IdentityStateStore(
        private_dir=Path(
            os.getenv("DRYCONTROLLER_IDENTITY_PRIVATE_DIR", "/var/lib/drycontroller-identity")
        ),
        active_device_path=Path(
            os.getenv("DRYCONTROLLER_DEVICE_IDENTITY_PATH", "/var/lib/drycontroller/device.json")
        ),
    )
    now = dt.datetime.now(dt.timezone.utc)
    active = _active_identity(store, now=now)
    status = build_status_payload(now=now)
    status["data"]["line"] = active["line_id"]
    encoded = base64.b64encode(
        json.dumps(status, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    payload = {
        "name": active["node_name"],
        "address": _tailnet_ipv4(),
        "edge_api_port": 8000,
        "line_id": active["line_id"],
        "system_status_b64": encoded,
        "reported_at": now.isoformat().replace("+00:00", "Z"),
        "meta": {"status_reporter": "device-identity-runtime"},
        "verify_cloud_to_edge_ready": verify_callback,
    }
    cloud_url = os.getenv("CLOUD_OPS_BASE_URL", "").strip()
    return UrlLibStatusCloudClient(cloud_url).report(
        payload,
        credential=active["runtime_credential"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Report Edge host status to Cloud Ops")
    parser.add_argument("--verify-callback", action="store_true")
    args = parser.parse_args()
    try:
        result = report_from_environment(verify_callback=args.verify_callback)
    except (StatusReportError, RuntimeError) as exc:
        print(f"Edge status report failed: {exc}")
        return 1
    print(json.dumps({"status": "reported", "node_id": result.get("node_id")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
