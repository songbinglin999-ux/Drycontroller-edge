"""
ws_stream 转发器单测（不依赖真实 MQTT/WS）

覆盖：
- payload 解析（dict/json/None/其他）
- latest cache（按 line + __latest__）
- predicate 过滤
- 队列满时丢弃最旧消息（_safe_put）
"""

import asyncio
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]
for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

from app.services.ws_stream import MQTTWebSocketRelay  # noqa: E402


@pytest.mark.asyncio
async def test_handle_message_updates_latest_and_broadcasts():
    relay = MQTTWebSocketRelay()
    relay.loop = asyncio.get_running_loop()
    relay.streams["realtime"] = {"pattern": "realtime/#", "subscribers": {}, "queue": asyncio.Queue()}

    q = relay.register_subscriber("realtime")

    relay._handle_message("realtime", "realtime/L1/tags", {"line": "L1", "tag": "T1", "value": 1})
    await asyncio.sleep(0)

    msg = q.get_nowait()
    assert msg["line"] == "L1"
    assert msg["topic"] == "realtime/L1/tags"
    assert relay.get_latest("realtime", "L1")["tag"] == "T1"
    assert relay.get_latest("realtime")["tag"] == "T1"


@pytest.mark.asyncio
async def test_handle_message_parses_json_string_and_skips_bad_json():
    relay = MQTTWebSocketRelay()
    relay.loop = asyncio.get_running_loop()
    relay.streams["system"] = {"pattern": "system/#", "subscribers": {}, "queue": asyncio.Queue()}
    q = relay.register_subscriber("system")

    relay._handle_message("system", "system/L1/status", '{"line":"L1","status":"healthy"}')
    await asyncio.sleep(0)
    msg = q.get_nowait()
    assert msg["status"] == "healthy"

    # bad json should not crash nor enqueue
    relay._handle_message("system", "system/L1/status", "{bad json")
    await asyncio.sleep(0)
    assert q.empty()


@pytest.mark.asyncio
async def test_predicate_filters_messages():
    relay = MQTTWebSocketRelay()
    relay.loop = asyncio.get_running_loop()
    relay.streams["system"] = {"pattern": "system/#", "subscribers": {}, "queue": asyncio.Queue()}

    q = relay.register_subscriber("system", predicate=lambda d: d.get("line") == "L2")
    relay._handle_message("system", "system/L1/status", {"line": "L1", "foo": 1})
    relay._handle_message("system", "system/L2/status", {"line": "L2", "foo": 2})
    await asyncio.sleep(0)

    msg = q.get_nowait()
    assert msg["foo"] == 2
    assert q.empty()


def test_safe_put_drops_oldest_when_queue_full():
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    MQTTWebSocketRelay._safe_put(q, {"n": 1})
    assert q.get_nowait()["n"] == 1

    # 塞满后再写入，应先 drop 再写入新消息
    MQTTWebSocketRelay._safe_put(q, {"n": 1})
    MQTTWebSocketRelay._safe_put(q, {"n": 2})
    assert q.get_nowait()["n"] == 2


def test_register_unknown_stream_raises():
    relay = MQTTWebSocketRelay()
    with pytest.raises(ValueError):
        relay.register_subscriber("unknown")

