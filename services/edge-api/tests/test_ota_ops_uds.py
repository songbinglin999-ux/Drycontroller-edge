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

from app.services import ota_ops  # noqa: E402


def _metadata(*, mode: int, uid: int = 0, gid: int = 0, nlink: int = 1):
    return SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid, st_nlink=nlink)


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        (_metadata(mode=stat.S_IFLNK | 0o777), "not_unix_socket"),
        (_metadata(mode=stat.S_IFREG | 0o660), "not_unix_socket"),
        (_metadata(mode=stat.S_IFSOCK | 0o660, uid=10001), "not_root_owned"),
        (_metadata(mode=stat.S_IFSOCK | 0o660, gid=10001), "unexpected_group"),
        (_metadata(mode=stat.S_IFSOCK | 0o666), "unexpected_mode"),
    ],
)
def test_ota_helper_socket_rejects_unsafe_metadata(metadata, reason):
    assert ota_ops._socket_metadata_error(metadata) == reason


def test_operation_id_cannot_escape_exact_helper_routes():
    with pytest.raises(HTTPException) as raised:
        ota_ops._validated_operation_id("../ready?override=true")
    assert raised.value.status_code == 422


def test_missing_helper_socket_is_503(monkeypatch, tmp_path):
    monkeypatch.setattr(ota_ops, "OTA_HELPER_SOCKET", tmp_path / "missing.sock")
    with pytest.raises(HTTPException) as raised:
        ota_ops._validated_helper_socket()
    assert raised.value.status_code == 503
    assert raised.value.detail == {"error": "ota_helper_unavailable", "reason": "socket_missing"}


def test_helper_path_uses_lstat_and_rejects_symlink(monkeypatch, tmp_path):
    regular = tmp_path / "regular.sock"
    regular.write_text("not a socket", encoding="utf-8")
    symlink = tmp_path / "symlink.sock"
    symlink.symlink_to(regular)
    monkeypatch.setattr(ota_ops, "OTA_HELPER_SOCKET", symlink)
    with pytest.raises(HTTPException) as raised:
        ota_ops._validated_helper_socket()
    assert raised.value.detail["reason"] == "not_unix_socket"


@pytest.mark.asyncio
async def test_edge_client_sends_only_operation_id_over_validated_uds(monkeypatch):
    socket_path = Path("/run/drycontroller-ota/helper.sock")
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

    monkeypatch.setattr(ota_ops, "_validated_helper_socket", lambda: socket_path)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", FakeTransport)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await ota_ops.OtaOpsClient().start("a" * 32)

    assert result["status"] == "queued"
    assert calls["uds"] == str(socket_path)
    assert calls["request"] == (
        "POST",
        "/ota/start",
        {"json": {"operation_id": "a" * 32}},
    )
