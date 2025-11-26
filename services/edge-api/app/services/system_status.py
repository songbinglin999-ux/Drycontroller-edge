"""
系统状态领域服务
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Optional

from app.core.config import settings
from app.services.influx import InfluxQueryService, get_influx_service
from app.services.ws_stream import ws_stream


class SystemStatusService:
    """封装 edge-monitor 推送的系统状态/指标访问逻辑"""

    def __init__(self, influx_service: Optional[InfluxQueryService] = None) -> None:
        self._influx = influx_service or get_influx_service()
        self._metrics_measurement = settings.SYSTEM_METRICS_MEASUREMENT

    def get_status_payload(self, line: Optional[str] = None) -> Optional[dict]:
        data = ws_stream.get_latest("system", line)
        if not data:
            return None
        return {
            "success": True,
            "data": data,
            "timestamp": int(time.time()),
        }

    def get_metrics_payload(self, line: Optional[str] = None) -> Optional[dict]:
        data = ws_stream.get_latest("system", line)
        if not data:
            return None
        metrics = data.get("system", {}).get("metrics", {})
        return {
            "success": True,
            "data": {
                "line": data.get("line"),
                "metrics": metrics,
                "services": data.get("services", []),
            },
            "timestamp": int(time.time()),
        }

    async def get_metric_trend(
        self,
        field: str,
        period: str,
        window: str,
        line: Optional[str] = None,
    ) -> dict:
        """查询 Influx 中的系统指标趋势"""
        trend = await self._influx.system_metric_trend(
            measurement=self._metrics_measurement,
            field=field,
            period=period,
            window=window,
            line=line,
        )
        return {
            "success": True,
            "data": trend,
            "count": len(trend),
            "timestamp": int(time.time()),
        }


@lru_cache(maxsize=1)
def get_system_status_service() -> SystemStatusService:
    return SystemStatusService()

