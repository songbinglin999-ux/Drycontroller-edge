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
TAILNET_HEADER = b"x-drycontroller-tailnet-ingress"


class ProxyRequestError(ValueError):
    pass


def mark_tailnet_request(header: bytes) -> bytes:
    if len(header) > HEADER_LIMIT or not header.endswith(b"\r\n\r\n"):
        raise ProxyRequestError("invalid HTTP header framing")
    lines = header[:-4].split(b"\r\n")
    if not lines or b" " not in lines[0]:
        raise ProxyRequestError("invalid HTTP request line")

    forwarded = [lines[0]]
    for line in lines[1:]:
        if not line or line[:1] in {b" ", b"\t"} or b":" not in line:
            raise ProxyRequestError("invalid HTTP request header")
        name, _value = line.split(b":", 1)
        normalized = name.strip().lower()
        if normalized == b"upgrade":
            raise ProxyRequestError("Tailnet websocket ingress is disabled")
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
    return b"\r\n".join(forwarded) + b"\r\n\r\n"


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while chunk := await reader.read(64 * 1024):
        writer.write(chunk)
        await writer.drain()


async def _handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    backend_writer: asyncio.StreamWriter | None = None
    upload: asyncio.Task[None] | None = None
    try:
        raw_header = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), timeout=10)
        marked_header = mark_tailnet_request(raw_header)
        backend_reader, backend_writer = await asyncio.wait_for(
            asyncio.open_connection(BACKEND_HOST, BACKEND_PORT),
            timeout=10,
        )
        backend_writer.write(marked_header)
        await backend_writer.drain()
        upload = asyncio.create_task(_copy(client_reader, backend_writer))
        await _copy(backend_reader, client_writer)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, OSError, ProxyRequestError):
        if backend_writer is None:
            client_writer.write(
                b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
            )
            with contextlib.suppress(OSError):
                await client_writer.drain()
    finally:
        if upload is not None:
            upload.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError):
                await upload
        if backend_writer is not None:
            backend_writer.close()
            with contextlib.suppress(OSError):
                await backend_writer.wait_closed()
        client_writer.close()
        with contextlib.suppress(OSError):
            await client_writer.wait_closed()


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
