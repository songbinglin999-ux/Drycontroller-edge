from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/bootstrap/tailnet-edge-api-proxy.py"


def _module():
    spec = importlib.util.spec_from_file_location("tailnet_edge_api_proxy", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_proxy_strips_spoofed_marker_and_forces_single_request_connection() -> None:
    proxy = _module()
    marked = proxy.mark_tailnet_request(
        b"GET /api/v1/data/latest HTTP/1.1\r\n"
        b"Host: edge.example\r\n"
        b"X-DryController-Tailnet-Ingress: forged\r\n"
        b"Connection: keep-alive\r\n\r\n"
    )

    assert marked.lower().count(b"x-drycontroller-tailnet-ingress:") == 1
    assert b"X-DryController-Tailnet-Ingress: 1\r\n" in marked
    assert b"Connection: close\r\n" in marked
    assert b"forged" not in marked
    assert b"keep-alive" not in marked.lower()


def test_proxy_rejects_tailnet_websocket_upgrade() -> None:
    proxy = _module()
    with pytest.raises(proxy.ProxyRequestError, match="websocket ingress is disabled"):
        proxy.mark_tailnet_request(
            b"GET /api/ws/realtime HTTP/1.1\r\n"
            b"Host: edge.example\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n\r\n"
        )


@pytest.mark.asyncio
async def test_proxy_marks_request_seen_by_loopback_backend() -> None:
    proxy = _module()
    observed: list[bytes] = []

    async def backend(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        observed.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    try:
        backend_server = await asyncio.start_server(backend, "127.0.0.1", 0)
    except PermissionError:
        pytest.skip("sandbox blocks loopback bind")
    proxy.BACKEND_PORT = backend_server.sockets[0].getsockname()[1]
    proxy_server = await asyncio.start_server(proxy._handle, "127.0.0.1", 0)
    try:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", proxy_server.sockets[0].getsockname()[1]
        )
        writer.write(
            b"GET /api/v1/data/latest HTTP/1.1\r\n"
            b"Host: edge.example\r\n"
            b"X-DryController-Tailnet-Ingress: forged\r\n\r\n"
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
    finally:
        proxy_server.close()
        backend_server.close()
        await proxy_server.wait_closed()
        await backend_server.wait_closed()

    assert response.endswith(b"ok")
    assert len(observed) == 1
    assert observed[0].lower().count(b"x-drycontroller-tailnet-ingress:") == 1
    assert b"X-DryController-Tailnet-Ingress: 1\r\n" in observed[0]


def test_installer_uses_marking_proxy_instead_of_blind_tcp_proxy() -> None:
    service = (ROOT / "infra/bootstrap/drycontroller-tailnet-edge-api.service").read_text()
    installer = (ROOT / "infra/bootstrap/install-edge-host.sh").read_text()

    assert "systemd-socket-proxyd" not in service
    assert "/usr/local/libexec/drycontroller/tailnet-edge-api-proxy.py" in service
    assert (
        'install -o root -g root -m 0755 "${REPO_SOURCE}/infra/bootstrap/tailnet-edge-api-proxy.py"'
        in installer
    )
