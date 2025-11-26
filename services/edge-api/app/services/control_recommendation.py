"""
控制建议领域服务（仅提供只读推荐）
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Dict, List, Optional

from app.services.realtime_data import (
    RealtimeDataService,
    get_realtime_data_service,
)


class ControlRecommendationService:
    TARGET_MOISTURE = 12.0
    CONTROL_TAGS = [
        "DRYER1.CHAIN.SPEED",
        "DRYER1.FAN.SPEED",
        "DRYER2.CHAIN.SPEED",
        "DRYER2.FAN.SPEED",
    ]

    def __init__(self, realtime_service: Optional[RealtimeDataService] = None) -> None:
        self._realtime = realtime_service or get_realtime_data_service()

    async def get_recommendations(
        self,
        line: Optional[str],
        lookback: str = "5m",
    ) -> dict:
        payload = await self._realtime.get_latest_payload(
            requested_tags=None,
            lookback=lookback,
        )
        latest = self._to_map(payload["data"])
        predictions = {
            "moisture_predicted": latest.get("MOISTURE.PREDICTED"),
            "confidence": latest.get("confidence"),
        }
        control = self._derive_recommendations(latest)
        return {
            "success": True,
            "data": {
                "line": line or "L1",
                "recommendations": control,
                "prediction": predictions,
                "timestamp": int(time.time()),
            },
        }

    def _derive_recommendations(self, latest: Dict[str, float]) -> Dict[str, float]:
        rec: Dict[str, float] = {}
        predicted = latest.get("MOISTURE.PREDICTED")
        delta = 0.0
        if predicted is not None:
            delta = predicted - self.TARGET_MOISTURE
        for tag in self.CONTROL_TAGS:
            value = latest.get(tag)
            if value is None:
                continue
            adjustment = self._compute_adjustment(tag, value, delta)
            rec[tag] = round(adjustment, 3)
        return rec

    @staticmethod
    def _to_map(records: List[dict]) -> Dict[str, float]:
        result: Dict[str, float] = {}
        for record in records:
            tag = record.get("tag")
            value = record.get("value")
            if tag is not None and value is not None:
                result[tag] = value
        return result

    def _compute_adjustment(self, tag: str, value: float, delta: float) -> float:
        # 简单策略：如果预测含水率高于目标，则增加风机/提高链速，反之降低。
        if "FAN" in tag:
            return value + delta * 0.5
        if "CHAIN" in tag:
            return value + delta * 0.2
        return value


@lru_cache(maxsize=1)
def get_control_recommendation_service() -> ControlRecommendationService:
    return ControlRecommendationService()

