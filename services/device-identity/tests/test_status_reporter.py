from __future__ import annotations

import datetime as dt
import hashlib
import json

import pytest

from identity_service.state import IdentityStateStore
from identity_service import status_reporter
from identity_service.status_reporter import (
    StatusReportError,
    _active_identity,
    _cpu_percent,
    _docker_services,
    _memory_percent,
    _tailnet_ipv4,
    _uptime,
)


NOW = dt.datetime(2026, 9, 4, 3, 0, tzinfo=dt.timezone.utc)


def _store(tmp_path, *, expires_at: dt.datetime):
    private_dir = tmp_path / "private"
    active_path = tmp_path / "runtime" / "device.json"
    private_dir.mkdir(mode=0o700, parents=True)
    active_path.parent.mkdir(mode=0o700, parents=True)
    credential = "device-secret"
    active_path.write_text(
        json.dumps(
            {
                "schema": "drycontroller.device-identity.v1",
                "request_id": "01" * 16,
                "device_id": "EDGE-ABCDEF123456",
                "tenant_id": "default",
                "line_id": "factory-cleanroom",
                "node_name": "edge-abcdef123456",
                "identity_fingerprint": "a" * 64,
                "runtime_credential": credential,
                "runtime_credential_hash": hashlib.sha256(credential.encode()).hexdigest(),
                "credential_expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )
    active_path.chmod(0o600)
    return IdentityStateStore(private_dir=private_dir, active_device_path=active_path)


def test_active_identity_requires_unexpired_matching_credential(tmp_path):
    store = _store(tmp_path, expires_at=NOW + dt.timedelta(hours=1))

    assert _active_identity(store, now=NOW)["device_id"] == "EDGE-ABCDEF123456"

    expired = _store(tmp_path / "expired", expires_at=NOW)
    with pytest.raises(StatusReportError, match="credential is expired"):
        _active_identity(expired, now=NOW)


def test_active_identity_rejects_tampered_credential(tmp_path):
    store = _store(tmp_path, expires_at=NOW + dt.timedelta(hours=1))
    payload = store.active()
    payload["runtime_credential"] = "tampered"
    store.active_device_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StatusReportError, match="identity is unavailable"):
        _active_identity(store, now=NOW)


def test_proc_metrics_are_bounded(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 1000 kB\nMemAvailable: 250 kB\n", encoding="ascii")
    uptime = tmp_path / "uptime"
    uptime.write_text("1234.75 100.0\n", encoding="ascii")

    assert _memory_percent(meminfo) == 75.0
    assert _uptime(uptime) == 1234


def test_cpu_averages_a_short_spike_over_five_seconds(monkeypatch):
    elapsed = 0.0

    def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds

    def cpu_times():
        # Eight CPUs: first 100 ms fully busy, then 10% busy.
        total = round(elapsed * 800)
        idle = round(max(0.0, elapsed - 0.1) * 800 * 0.9)
        return 10000 + total, 5000 + idle

    monkeypatch.setattr(status_reporter, "_cpu_times", cpu_times)
    assert _cpu_percent(sleep=sleep) == 11.8
    assert elapsed == 5.0


@pytest.mark.parametrize("after", [(100, 50), (99, 50), (200, 49)])
def test_cpu_rejects_invalid_counter_deltas(monkeypatch, after):
    samples = iter([(100, 50), after])
    monkeypatch.setattr(status_reporter, "_cpu_times", lambda: next(samples))
    with pytest.raises(StatusReportError, match="host CPU sample is invalid"):
        _cpu_percent(sleep=lambda _seconds: None)


def test_docker_services_preserve_health_and_failures():
    rows = [
        {
            "Names": "edge-api",
            "State": "running",
            "Status": "Up 2 hours (healthy)",
            "Labels": "com.docker.compose.project=drycontroller-edge",
        },
        {
            "Names": "edge-daq",
            "State": "running",
            "Status": "Up 2 hours (unhealthy)",
            "Labels": "com.docker.compose.project=drycontroller-edge",
        },
        {
            "Names": "edge-monitor",
            "State": "exited",
            "Status": "Exited (1)",
            "Labels": "com.docker.compose.project=drycontroller-edge",
        },
        {
            "Names": "fixture",
            "State": "running",
            "Status": "Up 1 hour",
            "Labels": "com.docker.compose.project=unrelated",
        },
        {
            "Names": "drycontroller-ipc-postgres",
            "State": "running",
            "Status": "Up 2 hours (healthy)",
            "Labels": "com.docker.compose.project=drycontroller-ipc-mainline",
        },
        {
            "Names": "drycontroller-ipc-hmi-runtime",
            "State": "exited",
            "Status": "Exited (1)",
            "Labels": "com.docker.compose.project=drycontroller-ipc-mainline",
        },
    ]

    observed = []

    def run(args):
        observed.append(args)
        return "\n".join(json.dumps(row) for row in rows)

    services = _docker_services(run)

    assert [(item["name"], item["status"]) for item in services] == [
        ("drycontroller-ipc-postgres", "running"),
        ("edge-api", "running"),
        ("edge-daq", "unhealthy"),
        ("edge-monitor", "down"),
    ]
    assert observed[0] == ["docker", "ps", "-a", "--format", "{{json .}}"]


def test_tailnet_address_accepts_only_cgnat_ipv4():
    assert _tailnet_ipv4(lambda _args: "192.168.1.10\n100.100.0.9\n") == "100.100.0.9"
    with pytest.raises(StatusReportError, match="Tailnet IPv4"):
        _tailnet_ipv4(lambda _args: "192.168.1.10\n")
