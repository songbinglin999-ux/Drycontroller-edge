"""
InfluxDB 关键事件写入工具。
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import time
from typing import Any, Dict, Optional

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from influxdb_client.rest import ApiException
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class _InfluxConfig:
    url: str
    bucket: str
    org: str
    token: str
    measurement: str


class EventRecorder:
    """
    将关键事件写入 InfluxDB，便于统一检索与告警。
    """

    def __init__(self, service_name: str):
        self.service_name = service_name
        self._config = self._build_config()
        self._client: InfluxDBClient | None = None
        self._write_api = None
        self._enabled = False

        if not self._config:
            logger.warning(
                "event_recorder_disabled",
                reason="missing_influx_config",
                service=service_name,
            )
            return

        try:
            self._client = InfluxDBClient(
                url=self._config.url,
                token=self._config.token,
                org=self._config.org,
                timeout=10_000,
            )
            self._write_api = self._client.write_api(write_options=SYNCHRONOUS)
            self._enabled = True
            logger.info(
                "event_recorder_enabled",
                service=service_name,
                measurement=self._config.measurement,
            )
        except (ApiException, OSError, RuntimeError, ValueError) as exc:  # pragma: no cover - 网络问题
            logger.error(
                "event_recorder_init_failed",
                error=str(exc),
                service=service_name,
            )
            self._client = None
            self._write_api = None
            self._enabled = False

    @staticmethod
    def _build_config() -> Optional[_InfluxConfig]:
        bucket = os.getenv("INFLUXDB_BUCKET")
        org = os.getenv("INFLUXDB_ORG")
        token = os.getenv("INFLUXDB_TOKEN")
        host = os.getenv("INFLUXDB_HOST", "localhost")
        port = os.getenv("INFLUXDB_PORT", "8086")
        measurement = os.getenv("INFLUXDB_EVENT_MEASUREMENT", "service_events")

        if not (bucket and org and token):
            return None

        return _InfluxConfig(
            url=f"http://{host}:{port}",
            bucket=bucket,
            org=org,
            token=token,
            measurement=measurement,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
            self._write_api = None
            self._enabled = False

    def __del__(self):
        # 析构阶段尽量避免 Influx SDK 抛出噪音异常
        try:
            self.close()
        except Exception:  # noqa: BLE001 - 故意宽捕：__del__ 内任何异常（含 Influx ApiException）都不得冒泡
            pass

    def record_event(
        self,
        event: str,
        *,
        severity: str = "info",
        tags: Optional[Dict[str, Any]] = None,
        fields: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        写入一条事件记录。返回写入是否成功。
        """
        if not self._write_api:
            return False

        payload = Point(self._config.measurement)
        payload.tag("service", self.service_name)
        payload.tag("event", event)
        payload.tag("severity", severity)

        for key, value in (tags or {}).items():
            payload.tag(key, str(value))

        normalized_fields = fields or {}
        if not normalized_fields:
            normalized_fields = {"value": 1}

        for key, value in normalized_fields.items():
            if isinstance(value, (int, float, bool)):
                payload.field(key, value)
            else:
                payload.field(key, str(value))

        payload.time(int(time.time() * 1e9))

        try:
            self._write_api.write(
                bucket=self._config.bucket,
                org=self._config.org,
                record=payload,
            )
            return True
        except (ApiException, OSError, RuntimeError, TypeError, ValueError) as exc:  # pragma: no cover - 网络问题
            logger.error(
                "event_record_failed",
                event_name=event,
                error=str(exc),
                service=self.service_name,
            )
            return False

