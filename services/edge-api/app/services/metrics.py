"""
Prometheus 指标（edge-api）。

目标：给 90 天游泳提供 P0 级可观测性：
- MQTT / event_bus / ws_stream 关键状态可被 scrape
- /metrics 每次请求时刷新快照，尽量避免后台线程复杂性
"""

from __future__ import annotations

from typing import Optional

from prometheus_client import Gauge

from app.core.mqtt_client import mqtt_manager
from app.services.event_bus import event_bus
from app.services.ws_stream import ws_stream


EDGE_API_UP = Gauge("edge_api_up", "Edge API process up")
EDGE_API_MQTT_CONNECTED = Gauge("edge_api_mqtt_connected", "Edge API MQTT connected state (1=connected)")
EDGE_API_EVENT_BUS_DROPPED_TOTAL = Gauge(
    "edge_api_event_bus_dropped_total",
    "Event bus dropped messages total (snapshot gauge)",
)
EDGE_API_EVENT_BUS_SUBSCRIPTIONS = Gauge(
    "edge_api_event_bus_subscriptions",
    "Event bus subscription pattern count (snapshot gauge)",
)
EDGE_API_WS_STREAM_STARTED = Gauge(
    "edge_api_ws_stream_started",
    "WS stream relay started (1=started)",
)
EDGE_API_WS_STREAM_SUBSCRIBERS = Gauge(
    "edge_api_ws_stream_subscribers",
    "WS stream subscribers count by stream",
    ["stream"],
)


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except Exception:
        return default


def refresh_metrics() -> None:
    """
    刷新 edge-api 内部状态指标。

    注意：/metrics 请求线程内执行，必须快速、无阻塞网络 IO。
    """
    EDGE_API_UP.set(1)
    EDGE_API_MQTT_CONNECTED.set(1 if mqtt_manager.connected else 0)

    stats = event_bus.get_stats()
    counters = stats.get("counters", {}) if isinstance(stats, dict) else {}
    EDGE_API_EVENT_BUS_DROPPED_TOTAL.set(_safe_int(counters.get("dropped", 0)))
    subs = stats.get("subscriptions", []) if isinstance(stats, dict) else []
    EDGE_API_EVENT_BUS_SUBSCRIPTIONS.set(len(subs) if isinstance(subs, list) else 0)

    EDGE_API_WS_STREAM_STARTED.set(1 if ws_stream.started else 0)
    # streams: {"realtime": {...}, "ai": {...}, "system": {...}}
    for name in ("realtime", "ai", "system"):
        stream = ws_stream.streams.get(name) if getattr(ws_stream, "streams", None) else None
        subscribers = stream.get("subscribers") if isinstance(stream, dict) else None
        EDGE_API_WS_STREAM_SUBSCRIBERS.labels(name).set(len(subscribers) if isinstance(subscribers, dict) else 0)


