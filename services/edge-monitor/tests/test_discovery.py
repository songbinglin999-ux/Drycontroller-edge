import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

PATH = Path(__file__).resolve()
ROOT = PATH.parents[3]
SERVICE_ROOT = PATH.parents[1]
LOCAL_LOG_ROOT = ROOT / "logs"
LOCAL_LOG_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOG_ROOT", str(LOCAL_LOG_ROOT))
for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

from monitor.discovery import DiscoveryAgent  # noqa: E402


def _make_agent(monkeypatch: pytest.MonkeyPatch, **env: str) -> DiscoveryAgent:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return DiscoveryAgent(line_id="L-test")


def test_build_payload_with_signature_and_extra(monkeypatch):
    agent = _make_agent(
        monkeypatch,
        DISCOVERY_SIGNING_KEY="secret",
        DISCOVERY_EXTRA_FIELDS='{"zone":"A1"}',
        DISCOVERY_BOOTSTRAP_TTL="45",
    )
    monkeypatch.setattr("monitor.discovery.time.time", lambda: 1_700_000_000)
    monkeypatch.setattr("monitor.discovery.secrets.token_hex", lambda _n: "deadbeef")

    payload = agent._build_payload()

    assert payload["line_id"] == "L-test"
    assert payload["generated_at"] == 1_700_000_000
    assert payload["expires_at"] == 1_700_000_045
    assert payload["nonce"] == "deadbeef"
    assert payload["extra"] == {"zone": "A1"}

    body = json.dumps(
        {k: payload[k] for k in sorted(payload) if k != "signature"},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    expected_signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert payload["signature"] == expected_signature


def test_build_mdns_properties_with_fallbacks():
    agent = DiscoveryAgent(line_id="L-fallback")

    payload: Dict[str, Any] = {}
    props = agent._build_mdns_properties(payload)

    assert props[b"api"] == agent.api_base.encode("utf-8")
    assert props[b"ws"] == agent.ws_base.encode("utf-8")
    assert props[b"line"] == b"L-fallback"

    payload.update(
        {
            "api_base_url": "http://custom/api",
            "ws_base_url": "ws://custom/api",
            "line_id": "L2",
            "version": "bootstrap.v2",
        }
    )
    props = agent._build_mdns_properties(payload)
    assert props[b"api"] == b"http://custom/api"
    assert props[b"ws"] == b"ws://custom/api"
    assert props[b"line"] == b"L2"
    assert props[b"v"] == b"bootstrap.v2"


def test_publish_mqtt_uses_supplier(monkeypatch):
    agent = _make_agent(monkeypatch, DISCOVERY_MQTT_TOPIC="system/bootstrap")
    published: Dict[str, Any] = {}

    class DummyClient:
        def publish(self, topic, payload, qos=0, retain=False):
            published.update({"topic": topic, "payload": payload, "qos": qos, "retain": retain})

    agent.set_mqtt_client_supplier(lambda: DummyClient())
    payload = agent._build_payload()
    agent._publish_mqtt(payload)

    assert published["topic"] == "system/bootstrap"
    body = json.loads(published["payload"])
    assert body["line_id"] == "L-test"
    assert published["qos"] == 1
    assert published["retain"] is True


@pytest.mark.asyncio
async def test_register_mdns_sets_state(monkeypatch):
    agent = DiscoveryAgent(line_id="L-mdns")
    recorded: Dict[str, Any] = {}

    monkeypatch.setattr(agent, "_resolve_mdns_address", lambda: b"\x7f\x00\x00\x01")

    class DummyAsyncZeroconf:
        async def async_register_service(self, info):
            recorded["info"] = info

        async def async_close(self):
            pass

    monkeypatch.setattr("monitor.discovery.AsyncZeroconf", lambda: DummyAsyncZeroconf())

    payload = agent._build_payload()
    await agent._register_mdns(payload)

    assert agent._mdns is not None
    assert agent._mdns_info is not None
    info = recorded["info"]
    assert info is not None
    assert info.name == agent.mdns_service_name
    assert info.server == agent.mdns_server

