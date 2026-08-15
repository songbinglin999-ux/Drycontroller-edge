from __future__ import annotations

import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException


PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]
for entry in (str(SERVICE_ROOT), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from app.services import host_release_ops  # noqa: E402


def _metadata(*, mode: int, uid: int = 0, gid: int = 0, nlink: int = 1):
    return SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid, st_nlink=nlink)


@pytest.mark.parametrize(
    "metadata",
    [
        _metadata(mode=stat.S_IFLNK | 0o777),
        _metadata(mode=stat.S_IFREG | 0o660),
        _metadata(mode=stat.S_IFSOCK | 0o660, uid=10001),
        _metadata(mode=stat.S_IFSOCK | 0o660, gid=10001),
        _metadata(mode=stat.S_IFSOCK | 0o666),
        _metadata(mode=stat.S_IFSOCK | 0o660, nlink=2),
    ],
)
def test_host_runner_socket_rejects_unsafe_metadata(monkeypatch, metadata):
    monkeypatch.setattr(Path, "lstat", lambda _self: metadata)
    with pytest.raises(HTTPException) as raised:
        host_release_ops._validated_socket()
    assert raised.value.status_code == 503
    assert raised.value.detail["reason"] == "socket_untrusted"


def test_operation_id_cannot_escape_runner_routes():
    with pytest.raises(HTTPException) as raised:
        host_release_ops._operation_id("../ready?override=true")
    assert raised.value.status_code == 422


@pytest.mark.asyncio
async def test_edge_client_sends_only_operation_id_over_validated_uds(monkeypatch):
    socket_path = Path("/run/drycontroller-host-release/runner.sock")
    calls: dict[str, object] = {}

    class FakeTransport:
        def __init__(self, *, uds, retries):
            calls["uds"] = uds
            calls["retries"] = retries

    class FakeResponse:
        status_code = 202

        def json(self):
            return {"operation_id": "a" * 32, "status": "queued"}

    class FakeClient:
        def __init__(self, *, transport, base_url, timeout, trust_env):
            calls["base_url"] = base_url
            calls["timeout"] = timeout
            calls["trust_env"] = trust_env

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, path, **kwargs):
            calls["request"] = (method, path, kwargs)
            return FakeResponse()

    monkeypatch.setattr(host_release_ops, "_validated_socket", lambda: socket_path)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", FakeTransport)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await host_release_ops.HostReleaseOpsClient().start("a" * 32)

    assert result["status"] == "queued"
    assert calls["uds"] == str(socket_path)
    assert calls["request"] == (
        "POST",
        "/host-release/start",
        {"json": {"operation_id": "a" * 32}},
    )


@pytest.mark.asyncio
async def test_edge_client_uses_fixed_read_only_tailnet_route(monkeypatch):
    socket_path = Path("/run/drycontroller-host-release/runner.sock")
    calls: dict[str, object] = {}

    class FakeTransport:
        def __init__(self, *, uds, retries):
            calls["uds"] = uds

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": True, "reason": "observed", "detail": {"node_id": "node-7"}}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, path, **kwargs):
            calls["request"] = (method, path, kwargs)
            return FakeResponse()

    monkeypatch.setattr(host_release_ops, "_validated_socket", lambda: socket_path)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", FakeTransport)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await host_release_ops.HostReleaseOpsClient().get_tailnet_status()

    assert result["detail"]["node_id"] == "node-7"
    assert calls["request"] == ("GET", "/tailnet/status", {})
