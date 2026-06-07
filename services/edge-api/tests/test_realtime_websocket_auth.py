import sys
import asyncio
import base64
import hashlib
import hmac
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]
for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

from app.api import realtime as realtime_router  # noqa: E402
from app.core.config import settings  # noqa: E402


def _build_client() -> TestClient:
    app = FastAPI()
    app.include_router(realtime_router.router, prefix="/api")
    return TestClient(app)


class DummyWsStream:
    def __init__(self):
        self.registered: list[tuple[str, object]] = []
        self.unregistered: list[str] = []

    def register_subscriber(self, stream_name: str, predicate=None):
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait({"stream": stream_name, "line": "L1", "tag": "FTT1", "value": 1})
        self.registered.append((stream_name, predicate))
        return queue

    def unregister_subscriber(self, stream_name: str, queue: asyncio.Queue):
        self.unregistered.append(stream_name)


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


def test_realtime_stream_rejects_missing_control_auth():
    old_control = list(settings.CONTROL_API_KEYS)
    old_timeout = realtime_router.WS_AUTH_TIMEOUT_SEC
    try:
        settings.CONTROL_API_KEYS = ["ctl-1"]
        realtime_router.WS_AUTH_TIMEOUT_SEC = 0.01
        client = _build_client()

        with client.websocket_connect("/api/realtime/stream") as websocket:
            try:
                websocket.receive_json()
            except WebSocketDisconnect as exc:
                assert exc.code == 1008
            else:
                raise AssertionError("websocket accepted missing auth")
    finally:
        realtime_router.WS_AUTH_TIMEOUT_SEC = old_timeout
        settings.CONTROL_API_KEYS = old_control


def test_realtime_stream_rejects_wrong_control_auth_token():
    old_control = list(settings.CONTROL_API_KEYS)
    try:
        settings.CONTROL_API_KEYS = ["ctl-1"]
        client = _build_client()

        with client.websocket_connect("/api/realtime/stream") as websocket:
            websocket.send_json({"type": "hello", "nonce": "nonce-1"})
            hello = websocket.receive_json()
            assert hello["type"] == "server_hello"
            websocket.send_json({"type": "auth", "token": "wrong"})
            try:
                websocket.receive_json()
            except WebSocketDisconnect as exc:
                assert exc.code == 1008
            else:
                raise AssertionError("websocket accepted wrong auth token")
    finally:
        settings.CONTROL_API_KEYS = old_control


def test_realtime_stream_rejects_legacy_direct_auth_frame():
    old_control = list(settings.CONTROL_API_KEYS)
    try:
        settings.CONTROL_API_KEYS = ["ctl-1"]
        client = _build_client()

        with client.websocket_connect("/api/realtime/stream") as websocket:
            websocket.send_json({"type": "auth", "token": "ctl-1"})
            try:
                websocket.receive_json()
            except WebSocketDisconnect as exc:
                assert exc.code == 1008
            else:
                raise AssertionError("websocket accepted auth without server identity hello")
    finally:
        settings.CONTROL_API_KEYS = old_control


class DummyWebSocket:
    def __init__(self, messages: list[str], client_host: str | None = None):
        self.messages = messages
        self.close_code = None
        self.sent: list[dict] = []
        self.client = type("Client", (), {"host": client_host})() if client_host else None

    async def receive_text(self):
        return self.messages.pop(0)

    async def send_json(self, payload: dict):
        self.sent.append(payload)

    async def close(self, code: int):
        self.close_code = code


@pytest.mark.asyncio
async def test_ws_auth_helper_accepts_control_key():
    old_control = list(settings.CONTROL_API_KEYS)
    try:
        settings.CONTROL_API_KEYS = ["ctl-1"]
        websocket = DummyWebSocket(
            [
                '{"type":"hello","nonce":"nonce-1"}',
                '{"type":"auth","token":"ctl-1"}',
            ]
        )

        assert await realtime_router._require_ws_control_auth(websocket) is True
        assert websocket.close_code is None
        assert websocket.sent[0]["type"] == "server_hello"
        assert websocket.sent[0]["nonce"] == "nonce-1"
        expected = base64.urlsafe_b64encode(
            hmac.new(b"ctl-1", b"nonce-1", hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")
        assert expected in websocket.sent[0]["proofs"]
        assert websocket.sent[1] == {"type": "auth_ok"}
    finally:
        settings.CONTROL_API_KEYS = old_control


@pytest.mark.asyncio
async def test_readonly_ws_auth_helper_rejects_non_allowlisted_source():
    old_allowlist = list(settings.READONLY_WS_ALLOWLIST)
    try:
        settings.READONLY_WS_ALLOWLIST = ["192.168.0.0/16"]
        websocket = DummyWebSocket([], client_host="203.0.113.10")

        assert await realtime_router._require_ws_readonly_auth(websocket) is False
        assert websocket.close_code == 1008
    finally:
        settings.READONLY_WS_ALLOWLIST = old_allowlist
