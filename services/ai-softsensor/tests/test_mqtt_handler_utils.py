"""
ai-softsensor MQTTHandler 工具方法测试
"""

import os
import sys
import types
import time
from collections import deque
from pathlib import Path


PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]

LOG_ROOT = Path("/tmp/drycontroller-logs")
LOG_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOG_ROOT", str(LOG_ROOT))

for entry in (str(SERVICE_ROOT), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

SRC_ROOT = SERVICE_ROOT / "src"
pkg = types.ModuleType("src")
pkg.__path__ = [str(SRC_ROOT)]
sys.modules["src"] = pkg

from src.mqtt_handler import MQTTHandler  # noqa: E402


class DummyModel:
    input_features = ["A", "B"]
    output_tag = "MOISTURE.PREDICTED"
    output_unit = "%"


def _make_handler():
    return MQTTHandler(DummyModel(), {"softsensor": {}})


def test_safe_float():
    assert MQTTHandler._safe_float("1.25") == 1.25
    assert MQTTHandler._safe_float(None) is None
    assert MQTTHandler._safe_float("bad") is None


def test_extract_batch_id():
    handler = _make_handler()
    handler.data_cache = {"BATCH.ID": 1001.0}
    batch_id = handler._extract_batch_id()
    assert batch_id == "1001.0"


def test_unbounded_buffers_do_not_compare_none_maxlen():
    handler = _make_handler()
    handler.mqtt_outbox = deque()
    handler.influx_buffer = deque()
    point = object()

    handler._buffer_mqtt_outbox("topic", "{}")
    handler._buffer_influx_point(point)

    assert list(handler.mqtt_outbox) == [("topic", "{}")]
    assert list(handler.influx_buffer) == [point]


def test_is_dq_ready_inference_age():
    handler = _make_handler()
    now = time.time()
    handler._last_inference_ts = now - 10
    handler._last_feature_max_age_sec = 1.0
    handler._last_frozen_features = []
    handler.ready_max_inference_age_sec = 5
    assert handler.is_dq_ready() is False

    handler.ready_max_inference_age_sec = 20
    assert handler.is_dq_ready() is True
