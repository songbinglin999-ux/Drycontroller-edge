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


class _MemoryWriter:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


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


@pytest.mark.parametrize(
    "framing",
    (
        b"Transfer-Encoding: chunked\r\n",
        b"Expect: 100-continue\r\n",
        b"Content-Length: 1\r\nContent-Length: 1\r\n",
        b"Content-Length: 999999999\r\n",
    ),
)
def test_proxy_rejects_ambiguous_or_unbounded_request_framing(framing: bytes) -> None:
    proxy = _module()
    with pytest.raises(proxy.ProxyRequestError):
        proxy.mark_tailnet_request(
            b"POST /api/v1/ops/preflight HTTP/1.1\r\n"
            b"Host: edge.example\r\n"
            + framing
            + b"\r\n"
        )


@pytest.mark.asyncio
async def test_proxy_forwards_only_the_declared_single_request(monkeypatch) -> None:
    proxy = _module()
    client_reader = asyncio.StreamReader()
    client_reader.feed_data(
        b"POST /api/v1/ops/preflight HTTP/1.1\r\n"
        b"Host: edge.example\r\n"
        b"Content-Length: 4\r\n\r\n"
        b"data"
        b"GET /metrics HTTP/1.1\r\nHost: edge.example\r\n\r\n"
    )
    client_reader.feed_eof()
    client_writer = _MemoryWriter()
    backend_reader = asyncio.StreamReader()
    backend_reader.feed_data(
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
    )
    backend_reader.feed_eof()
    backend_writer = _MemoryWriter()

    async def open_backend(_host: str, _port: int):
        return backend_reader, backend_writer

    monkeypatch.setattr(proxy.asyncio, "open_connection", open_backend)
    await proxy._handle(client_reader, client_writer)

    assert bytes(backend_writer.data).endswith(b"\r\n\r\ndata")
    assert b"GET /metrics" not in backend_writer.data
    assert bytes(client_writer.data).endswith(b"ok")


@pytest.mark.asyncio
async def test_proxy_does_not_open_backend_for_truncated_body(monkeypatch) -> None:
    proxy = _module()
    client_reader = asyncio.StreamReader()
    client_reader.feed_data(
        b"POST /api/v1/ops/preflight HTTP/1.1\r\n"
        b"Host: edge.example\r\n"
        b"Content-Length: 1000000\r\n\r\n"
        b"partial"
    )
    client_reader.feed_eof()
    client_writer = _MemoryWriter()
    backend_opened = False

    async def open_backend(_host: str, _port: int):
        nonlocal backend_opened
        backend_opened = True
        raise AssertionError("backend must not be opened for an incomplete request")

    monkeypatch.setattr(proxy.asyncio, "open_connection", open_backend)
    await proxy._handle(client_reader, client_writer)

    assert backend_opened is False
    assert bytes(client_writer.data).startswith(b"HTTP/1.1 400 Bad Request")


@pytest.mark.asyncio
async def test_proxy_times_out_incomplete_request_and_releases_slot() -> None:
    proxy = _module()
    proxy.REQUEST_READ_TIMEOUT = 0.01
    client_writer = _MemoryWriter()

    await proxy._handle(asyncio.StreamReader(), client_writer)

    assert bytes(client_writer.data).startswith(b"HTTP/1.1 400 Bad Request")
    assert proxy._active_requests == 0


@pytest.mark.asyncio
async def test_proxy_rejects_connections_over_the_concurrency_limit() -> None:
    proxy = _module()
    proxy._active_requests = proxy.MAX_CONCURRENT_REQUESTS
    client_writer = _MemoryWriter()

    await proxy._handle(asyncio.StreamReader(), client_writer)

    assert bytes(client_writer.data).startswith(b"HTTP/1.1 503 Service Unavailable")
    assert client_writer.closed is True


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
