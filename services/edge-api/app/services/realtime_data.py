"""
实时/历史数据领域服务
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import List, Optional

from app.services.influx import InfluxQueryService, get_influx_service
from app.utils.tag_loader import get_all_tag_metadata, get_all_tag_names


class RealtimeDataService:
    """封装实时/历史标签查询逻辑，供 REST 与 WS 复用"""

    def __init__(self, influx_service: Optional[InfluxQueryService] = None) -> None:
        self._influx = influx_service or get_influx_service()

    @property
    def influx_service(self) -> InfluxQueryService:
        """暴露底层 Influx service，供其他领域服务扩展查询"""
        return self._influx

    def resolve_tags(self, requested: Optional[List[str]]) -> List[str]:
        """若未指定则返回全量配置标签"""
        if requested:
            return [tag.strip() for tag in requested if tag and tag.strip()]
        return get_all_tag_names()

    async def get_latest_payload(
        self,
        requested_tags: Optional[List[str]],
        lookback: str,
    ) -> dict:
        tag_list = self.resolve_tags(requested_tags)
        if not tag_list:
            return self._empty_latest_response()

        points = await self._influx.latest_points(tag_list, lookback=lookback)
        payload_tags = {point.get("tag") for point in points if point.get("tag")}
        missing = sorted(set(tag_list) - payload_tags)

        return {
            "success": True,
            "data": points,
            "missing": missing,
            "count": len(points),
            "timestamp": int(time.time()),
        }

    async def get_history_payload(
        self,
        tag: str,
        period: str,
        window: str,
        aggregation: str,
    ) -> dict:
        values = await self._influx.history_points(
            tag=tag,
            period=period,
            window=window,
            aggregation=aggregation,
        )
        return {
            "success": True,
            "data": {"tag": tag, "points": values},
            "count": len(values),
            "timestamp": int(time.time()),
        }

    def get_tags_payload(self) -> dict:
        tags = get_all_tag_metadata()
        return {
            "success": True,
            "data": {"tags": tags, "count": len(tags)},
            "timestamp": int(time.time()),
        }

    @staticmethod
    def _empty_latest_response() -> dict:
        return {
            "success": True,
            "data": [],
            "missing": [],
            "count": 0,
            "timestamp": int(time.time()),
        }


@lru_cache(maxsize=1)
def get_realtime_data_service() -> RealtimeDataService:
    return RealtimeDataService()
