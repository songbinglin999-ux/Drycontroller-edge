import sys
import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]
for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

from app.api import realtime as realtime_router  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.security import (  # noqa: E402
    TAILNET_INGRESS_HEADER,
    is_readonly_source_allowed,
    require_readonly_ws_source,
)


def _build_client() -> TestClient:
    app = FastAPI()
    app.include_router(realtime_router.router, prefix="/api")
    return TestClient(app)


class DummyWsStream:
    def __init__(self):
        self.registered: list[tuple[str, object]] = []
        self.unregistered: list[str] = []
        self.latest: dict | None = None

    def register_subscriber(self, stream_name: str, predicate=None):
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait({"stream": stream_name, "line": "L1", "tag": "FTT1", "value": 1})
        self.registered.append((stream_name, predicate))
        return queue

    def unregister_subscriber(self, stream_name: str, queue: asyncio.Queue):
        self.unregistered.append(stream_name)

    def get_latest(self, stream_name: str, line=None):
        return self.latest


@pytest.mark.parametrize(
    ("path", "stream_name"),
    [
        ("/api/ws/realtime?line=L1&tags=FTT1", "realtime"),
        ("/api/ws/ai?line=L1", "ai"),
        ("/api/ws/system?line=L1", "system"),
    ],
)
def test_readonly_ws_streams_allow_allowlisted_client_without_control_auth(monkeypatch, path, stream_name):
    old_allowlist = list(settings.READONLY_WS_ALLOWLIST)
    try:
        settings.READONLY_WS_ALLOWLIST = ["testclient"]
        dummy_stream = DummyWsStream()
        monkeypatch.setattr(realtime_router, "ws_stream", dummy_stream)
        client = _build_client()

        with client.websocket_connect(path) as websocket:
            payload = websocket.receive_json()
            assert payload["stream"] == stream_name
            assert payload["line"] == "L1"

        assert dummy_stream.registered[0][0] == stream_name
    finally:
        settings.READONLY_WS_ALLOWLIST = old_allowlist


class DummyWebSocket:
    def __init__(
        self,
        messages: list[str],
        client_host: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.messages = messages
        self.close_code = None
        self.sent: list[dict] = []
        self.client = type("Client", (), {"host": client_host})() if client_host else None
        self.headers = headers or {}

    async def receive_text(self):
        return self.messages.pop(0)

    async def send_json(self, payload: dict):
        self.sent.append(payload)

    async def close(self, code: int):
        self.close_code = code


@pytest.mark.asyncio
async def test_readonly_ws_auth_helper_rejects_non_allowlisted_source():
    old_allowlist = list(settings.READONLY_WS_ALLOWLIST)
    try:
        settings.READONLY_WS_ALLOWLIST = ["192.168.0.0/16"]
        websocket = DummyWebSocket([], client_host="203.0.113.10")

        assert await require_readonly_ws_source(websocket) is False
        assert websocket.close_code == 1008
    finally:
        settings.READONLY_WS_ALLOWLIST = old_allowlist


@pytest.mark.asyncio
async def test_tailnet_proxy_marker_rejects_allowlisted_websocket_source():
    old_allowlist = list(settings.READONLY_WS_ALLOWLIST)
    try:
        settings.READONLY_WS_ALLOWLIST = ["172.16.0.0/12"]
        websocket = DummyWebSocket(
            [],
            client_host="172.18.0.1",
            headers={TAILNET_INGRESS_HEADER: "1"},
        )

        assert await require_readonly_ws_source(websocket) is False
        assert websocket.close_code == 1008
    finally:
        settings.READONLY_WS_ALLOWLIST = old_allowlist


def test_readonly_http_endpoint_rejects_non_allowlisted_source():
    old_allowlist = list(settings.READONLY_WS_ALLOWLIST)
    try:
        settings.READONLY_WS_ALLOWLIST = ["192.168.0.0/16"]
        client = _build_client()

        resp = client.get("/api/system/health")

        assert resp.status_code == 403
        assert resp.json()["detail"] == "Read-only data access is not allowed from this source"
    finally:
        settings.READONLY_WS_ALLOWLIST = old_allowlist


def test_tailnet_address_is_not_readonly_authorization_but_api_key_is(monkeypatch):
    old_allowlist = list(settings.READONLY_WS_ALLOWLIST)
    old_control = list(settings.CONTROL_API_KEYS)
    old_admin = list(settings.ADMIN_API_KEYS)
    try:
        settings.READONLY_WS_ALLOWLIST = ["127.0.0.1/32", "192.168.0.0/16"]
        settings.CONTROL_API_KEYS = []
        settings.ADMIN_API_KEYS = ["admin-1"]
        assert is_readonly_source_allowed("100.100.0.9") is False
        dummy_stream = DummyWsStream()
        dummy_stream.latest = {"line": "L1", "status": "ok"}
        monkeypatch.setattr(realtime_router, "ws_stream", dummy_stream)

        client = _build_client()
        tailnet_only = client.get(
            "/api/system/health",
            headers={"Tailscale-User-Login": "operator@example.test"},
        )
        authenticated = client.get(
            "/api/system/health",
            headers={settings.API_KEY_HEADER: "admin-1"},
        )

        assert tailnet_only.status_code == 403
        assert authenticated.status_code == 200
    finally:
        settings.READONLY_WS_ALLOWLIST = old_allowlist
        settings.CONTROL_API_KEYS = old_control
        settings.ADMIN_API_KEYS = old_admin


def test_readonly_http_endpoint_allows_allowlisted_source(monkeypatch):
    old_allowlist = list(settings.READONLY_WS_ALLOWLIST)
    try:
        settings.READONLY_WS_ALLOWLIST = ["testclient"]
        dummy_stream = DummyWsStream()
        dummy_stream.latest = {"line": "L1", "status": "ok"}
        monkeypatch.setattr(realtime_router, "ws_stream", dummy_stream)
        client = _build_client()

        resp = client.get("/api/system/health")

        assert resp.status_code == 200
        assert resp.json()["data"] == {"line": "L1", "status": "ok"}
    finally:
        settings.READONLY_WS_ALLOWLIST = old_allowlist


def test_tailnet_proxy_marker_disables_private_source_bypass(monkeypatch):
    old_allowlist = list(settings.READONLY_WS_ALLOWLIST)
    old_admin = list(settings.ADMIN_API_KEYS)
    try:
        settings.READONLY_WS_ALLOWLIST = ["testclient", "172.16.0.0/12"]
        settings.ADMIN_API_KEYS = ["admin-1"]
        dummy_stream = DummyWsStream()
        dummy_stream.latest = {"line": "L1", "status": "ok"}
        monkeypatch.setattr(realtime_router, "ws_stream", dummy_stream)
        client = _build_client()

        unsigned = client.get(
            "/api/system/health",
            headers={TAILNET_INGRESS_HEADER: "1"},
        )
        authenticated = client.get(
            "/api/system/health",
            headers={TAILNET_INGRESS_HEADER: "1", settings.API_KEY_HEADER: "admin-1"},
        )

        assert unsigned.status_code == 403
        assert authenticated.status_code == 200
    finally:
        settings.READONLY_WS_ALLOWLIST = old_allowlist
        settings.ADMIN_API_KEYS = old_admin
