#!/usr/bin/python3
"""Minimal TLS policy endpoint for the isolated formal-install OTA E2E."""

from __future__ import annotations

import argparse
import hmac
import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MAX_BYTES = 1024 * 1024


def _read(path: Path, maximum: int = MAX_BYTES) -> bytes:
    data = path.read_bytes()
    if not data or len(data) > maximum:
        raise ValueError(f"fixture file invalid: {path.name}")
    return data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--credential", required=True, type=Path)
    parser.add_argument("--certificate", required=True, type=Path)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()

    policy = _read(args.policy)
    parsed = json.loads(policy)
    if parsed.get("operation_id") != args.operation_id or len(args.operation_id) != 32:
        raise ValueError("policy operation binding invalid")
    credential = _read(args.credential, 4096).decode("utf-8").strip()
    if not credential:
        raise ValueError("device credential fixture invalid")

    route = f"/api/v1/ota/release-policy/{args.operation_id}"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_values: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            authorized = hmac.compare_digest(
                self.headers.get("X-Device-Credential", ""), credential
            )
            if self.path != route or not authorized:
                self.send_response(404 if authorized else 401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(policy)))
            self.end_headers()
            self.wfile.write(policy)

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.certificate, args.private_key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()
