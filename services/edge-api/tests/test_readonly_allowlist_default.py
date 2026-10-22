"""The read-only data plane must default to loopback-only sources."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402


def test_readonly_ws_allowlist_defaults_to_loopback_only():
    default = Settings.model_fields["READONLY_WS_ALLOWLIST"].default
    assert default == ["127.0.0.1/32", "::1/128"]
