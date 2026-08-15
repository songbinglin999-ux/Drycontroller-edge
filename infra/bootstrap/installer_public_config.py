"""Validation shared by RB-6 media checks and target initialization."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit


SCHEMA = "drycontroller.os-installer-public-config.v2"
FIELDS = {
    "schema",
    "cloud_ops_base_url",
    "tailscale_login_server",
    "factory_interface",
    "factory_address",
}
INTERFACE = re.compile(r"^(?=.{3,15}$)(?:en|eth)[A-Za-z0-9_.-]+$")
HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _canonical_https_base(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("https://"):
        raise ValueError(f"OS Installer {label} must be canonical HTTPS")
    if any(character.isspace() for character in value) or "?" in value or "#" in value:
        raise ValueError(f"OS Installer {label} must not contain whitespace, query, or fragment")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"OS Installer {label} authority invalid") from exc
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise ValueError(f"OS Installer {label} must be one canonical HTTPS base URL")
    labels = hostname.split(".")
    if len(hostname) > 253 or any(HOST_LABEL.fullmatch(label) is None for label in labels):
        raise ValueError(f"OS Installer {label} hostname invalid")
    expected_authority = hostname if port is None else f"{hostname}:{port}"
    if parsed.netloc != expected_authority:
        raise ValueError(f"OS Installer {label} authority is not canonical")
    path_segments = parsed.path.split("/")
    if "//" in parsed.path or "\\" in parsed.path or any(
        segment in {".", ".."} for segment in path_segments
    ):
        raise ValueError(f"OS Installer {label} path invalid")
    return value.rstrip("/")


def validate_public_config(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != FIELDS or payload.get("schema") != SCHEMA:
        raise ValueError("OS Installer public config fields invalid")
    interface = payload["factory_interface"]
    if not isinstance(interface, str) or INTERFACE.fullmatch(interface) is None:
        raise ValueError("OS Installer factory interface must use one predictable Ethernet name")
    if payload["factory_address"] != "169.254.203.2/30":
        raise ValueError("OS Installer factory address must use the frozen link-local channel")
    return {
        **payload,
        "cloud_ops_base_url": _canonical_https_base(payload["cloud_ops_base_url"], "Cloud URL"),
        "tailscale_login_server": _canonical_https_base(
            payload["tailscale_login_server"], "Tailscale login server"
        ),
    }
