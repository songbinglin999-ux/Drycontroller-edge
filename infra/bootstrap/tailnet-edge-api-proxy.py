#!/usr/bin/env python3
"""Mark socket-activated Tailnet HTTP requests before proxying to Edge API."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket


BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 18080
HEADER_LIMIT = 64 * 1024
BODY_LIMIT = 1024 * 1024
REQUEST_READ_TIMEOUT = 15
BACKEND_RESPONSE_TIMEOUT = 120
MAX_CONCURRENT_REQUESTS = 64
TAILNET_HEADER = b"x-drycontroller-tailnet-ingress"
_active_requests = 0


class ProxyRequestError(ValueError):
    pass


def _prepare_tailnet_request(header: bytes) -> tuple[bytes, int]:
    if len(header) > HEADER_LIMIT or not header.endswith(b"\r\n\r\n"):
        raise ProxyRequestError("invalid HTTP header framing")
    lines = header[:-4].split(b"\r\n")
    request_parts = lines[0].split(b" ") if lines else []
    if len(request_parts) != 3 or request_parts[2] not in {b"HTTP/1.0", b"HTTP/1.1"}:
        raise ProxyRequestError("invalid HTTP request line")

    forwarded = [lines[0]]
    content_length: int | None = None
    for line in lines[1:]:
        if not line or line[:1] in {b" ", b"\t"} or b":" not in line:
            raise ProxyRequestError("invalid HTTP request header")
        name, value = line.split(b":", 1)
        normalized = name.strip().lower()
        if normalized == b"upgrade":
            raise ProxyRequestError("Tailnet websocket ingress is disabled")
        if normalized in {b"transfer-encoding", b"expect"}:
            raise ProxyRequestError("unsupported HTTP request framing")
        if normalized == b"content-length":
            raw_length = value.strip()
            if (
                content_length is not None
                or not raw_length.isdigit()
                or len(raw_length) > len(str(BODY_LIMIT))
            ):
                raise ProxyRequestError("invalid HTTP content length")
            content_length = int(raw_length)
            if content_length > BODY_LIMIT:
                raise ProxyRequestError("HTTP request body exceeds the limit")
        if normalized in {
            TAILNET_HEADER,
            b"connection",
            b"keep-alive",
            b"proxy-connection",
        }:
            continue
        forwarded.append(line)
    forwarded.extend(
        (
            b"X-DryController-Tailnet-Ingress: 1",
            b"Connection: close",
        )
    )
    return b"\r\n".join(forwarded) + b"\r\n\r\n", content_length or 0


def mark_tailnet_request(header: bytes) -> bytes:
    return _prepare_tailnet_request(header)[0]


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while chunk := await reader.read(64 * 1024):
        writer.write(chunk)
        await writer.drain()


async def _read_request(client_reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    raw_header = await client_reader.readuntil(b"\r\n\r\n")
    marked_header, content_length = _prepare_tailnet_request(raw_header)
    body = await client_reader.readexactly(content_length)
    return marked_header, body


async def _handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    global _active_requests
    if _active_requests >= MAX_CONCURRENT_REQUESTS:
        client_writer.write(
            b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
        )
        with contextlib.suppress(OSError):
            await client_writer.drain()
        client_writer.close()
        with contextlib.suppress(OSError):
            await client_writer.wait_closed()
        return

    _active_requests += 1
    backend_writer: asyncio.StreamWriter | None = None
    try:
        marked_header, body = await asyncio.wait_for(
            _read_request(client_reader),
            timeout=REQUEST_READ_TIMEOUT,
        )
        backend_reader, backend_writer = await asyncio.wait_for(
            asyncio.open_connection(BACKEND_HOST, BACKEND_PORT),
            timeout=REQUEST_READ_TIMEOUT,
        )
        backend_writer.write(marked_header)
        if body:
            backend_writer.write(body)
        await backend_writer.drain()
        await asyncio.wait_for(
            _copy(backend_reader, client_writer),
            timeout=BACKEND_RESPONSE_TIMEOUT,
        )
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, OSError, ProxyRequestError):
        if backend_writer is None:
            client_writer.write(
                b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
            )
            with contextlib.suppress(OSError):
                await client_writer.drain()
    finally:
        if backend_writer is not None:
            backend_writer.close()
            with contextlib.suppress(OSError):
                await backend_writer.wait_closed()
        client_writer.close()
        with contextlib.suppress(OSError):
            await client_writer.wait_closed()
        _active_requests -= 1


def _socket_activation_listener() -> socket.socket:
    if int(os.environ.get("LISTEN_PID", "0")) != os.getpid():
        raise RuntimeError("socket activation PID mismatch")
    if int(os.environ.get("LISTEN_FDS", "0")) != 1:
        raise RuntimeError("exactly one socket activation descriptor is required")
    listener = socket.socket(fileno=3)
    listener.setblocking(False)
    return listener


async def _main() -> None:
    server = await asyncio.start_server(_handle, sock=_socket_activation_listener(), limit=HEADER_LIMIT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_main())
