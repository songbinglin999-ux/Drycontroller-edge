import sys
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


def test_ws_realtime_rejects_missing_auth():
    old_control = list(settings.CONTROL_API_KEYS)
    old_timeout = realtime_router.WS_AUTH_TIMEOUT_SEC
    try:
        settings.CONTROL_API_KEYS = ["ctl-1"]
        realtime_router.WS_AUTH_TIMEOUT_SEC = 0.01
        client = _build_client()

        with client.websocket_connect("/api/ws/realtime") as websocket:
            try:
                websocket.receive_json()
            except WebSocketDisconnect as exc:
                assert exc.code == 1008
            else:
                raise AssertionError("websocket accepted missing auth")
    finally:
        realtime_router.WS_AUTH_TIMEOUT_SEC = old_timeout
        settings.CONTROL_API_KEYS = old_control


def test_ws_realtime_rejects_wrong_auth_token():
    old_control = list(settings.CONTROL_API_KEYS)
    try:
        settings.CONTROL_API_KEYS = ["ctl-1"]
        client = _build_client()

        with client.websocket_connect("/api/ws/realtime") as websocket:
            websocket.send_json({"type": "auth", "token": "wrong"})
            try:
                websocket.receive_json()
            except WebSocketDisconnect as exc:
                assert exc.code == 1008
            else:
                raise AssertionError("websocket accepted wrong auth token")
    finally:
        settings.CONTROL_API_KEYS = old_control


class DummyWebSocket:
    def __init__(self, message: str):
        self.message = message
        self.close_code = None

    async def receive_text(self):
        return self.message

    async def close(self, code: int):
        self.close_code = code


@pytest.mark.asyncio
async def test_ws_auth_helper_accepts_control_key():
    old_control = list(settings.CONTROL_API_KEYS)
    try:
        settings.CONTROL_API_KEYS = ["ctl-1"]
        websocket = DummyWebSocket('{"type":"auth","token":"ctl-1"}')

        assert await realtime_router._require_ws_control_auth(websocket) is True
        assert websocket.close_code is None
    finally:
        settings.CONTROL_API_KEYS = old_control
