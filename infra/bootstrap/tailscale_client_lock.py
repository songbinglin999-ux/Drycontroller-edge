"""Validate the public Tailscale client lock embedded in release media."""

from __future__ import annotations

import re


SCHEMA = "drycontroller.tailscale-client.v1"
FIELDS = {"schema", "version", "architecture", "deb_url", "deb_sha256"}
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def validate_tailscale_client_lock(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict) or set(payload) != FIELDS:
        raise ValueError("Tailscale client lock fields invalid")
    if payload.get("schema") != SCHEMA:
        raise ValueError("Tailscale client lock schema invalid")
    version = payload.get("version")
    architecture = payload.get("architecture")
    deb_url = payload.get("deb_url")
    deb_sha256 = payload.get("deb_sha256")
    if not isinstance(version, str) or VERSION.fullmatch(version) is None:
        raise ValueError("Tailscale client version invalid")
    if architecture != "amd64":
        raise ValueError("Tailscale client architecture must be amd64")
    expected_url = (
        "https://pkgs.tailscale.com/stable/ubuntu/pool/"
        f"tailscale_{version}_amd64.deb"
    )
    if deb_url != expected_url:
        raise ValueError("Tailscale client DEB URL is not canonical")
    if not isinstance(deb_sha256, str) or SHA256.fullmatch(deb_sha256) is None:
        raise ValueError("Tailscale client DEB SHA-256 invalid")
    return {
        "schema": SCHEMA,
        "version": version,
        "architecture": architecture,
        "deb_url": deb_url,
        "deb_sha256": deb_sha256,
    }


def from_headscale_versions_lock(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("Headscale versions lock invalid")
    client = payload.get("tailscale_client")
    if not isinstance(client, dict):
        raise ValueError("Headscale versions lock has no Tailscale client section")
    candidate = client.get("linux_amd64_candidate")
    if not isinstance(candidate, dict) or set(candidate) != {
        "version",
        "deb_url",
        "deb_sha256",
    }:
        raise ValueError("Headscale versions lock Tailscale candidate invalid")
    return validate_tailscale_client_lock(
        {
            "schema": SCHEMA,
            "version": candidate.get("version"),
            "architecture": "amd64",
            "deb_url": candidate.get("deb_url"),
            "deb_sha256": candidate.get("deb_sha256"),
        }
    )
