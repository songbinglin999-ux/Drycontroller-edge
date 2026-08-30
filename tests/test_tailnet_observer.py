from __future__ import annotations

import datetime as dt
import importlib.util
import json
import stat
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tailnet_observer_writes_only_sanitized_observation(tmp_path, monkeypatch):
    module = _load(ROOT / "scripts/observe-tailnet-status.py", "observe_tailnet_status")
    monkeypatch.setattr(
        module,
        "read_tailscale_status",
        lambda: {"ok": True, "reason": "observed", "detail": {"hostname": "edge-a"}},
    )
    monkeypatch.setattr(module, "_edge_api_ingress_active", lambda: True)
    output = tmp_path / "status.json"

    payload = module.observe(output)

    assert payload["schema"] == "drycontroller.tailnet-observation.v1"
    assert payload["observation"]["detail"] == {
        "edge_api_ingress_active": True,
        "hostname": "edge-a",
    }
    assert stat.S_IMODE(output.stat().st_mode) == 0o444


def test_edge_api_loader_rejects_untrusted_or_extra_fields(tmp_path, monkeypatch):
    module = _load(
        ROOT / "services/edge-api/app/services/tailnet_observation.py",
        "tailnet_observation",
    )
    path = tmp_path / "status.json"
    payload = {
        "schema": "drycontroller.tailnet-observation.v1",
        "observed_at": "2026-08-30T00:00:00Z",
        "observation": {"ok": True, "reason": "observed", "detail": {}},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    actual = path.lstat()
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda self: type(
            "Metadata",
            (),
            {"st_mode": stat.S_IFREG | 0o444, "st_uid": 0, "st_size": actual.st_size},
        )(),
    )

    now = dt.datetime(2026, 8, 30, 0, 1, tzinfo=dt.UTC)
    assert module.load_tailnet_observation(path, now=now) == payload["observation"]
    payload["unexpected"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid"):
        module.load_tailnet_observation(path, now=now)


def test_edge_api_loader_rejects_stale_observation(tmp_path, monkeypatch):
    module = _load(
        ROOT / "services/edge-api/app/services/tailnet_observation.py",
        "stale_tailnet_observation",
    )
    path = tmp_path / "status.json"
    path.write_text(
        json.dumps(
            {
                "schema": "drycontroller.tailnet-observation.v1",
                "observed_at": "2026-08-30T00:00:00Z",
                "observation": {"ok": True, "reason": "observed", "detail": {}},
            }
        ),
        encoding="utf-8",
    )
    actual = path.lstat()
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda self: type(
            "Metadata",
            (),
            {"st_mode": stat.S_IFREG | 0o444, "st_uid": 0, "st_size": actual.st_size},
        )(),
    )

    with pytest.raises(ValueError, match="stale"):
        module.load_tailnet_observation(
            path,
            now=dt.datetime(2026, 8, 30, 0, 3, 1, tzinfo=dt.UTC),
        )


def test_active_edge_api_has_no_host_release_runtime_caller():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    ops = (ROOT / "services/edge-api/app/api/v1/ops.py").read_text(encoding="utf-8")
    system = (ROOT / "services/edge-api/app/api/v1/system.py").read_text(encoding="utf-8")

    assert "/run/drycontroller-host-release" not in compose
    assert "host_release_ops" not in ops
    assert "host_release_ops" not in system
    assert "/host-release/" not in ops
