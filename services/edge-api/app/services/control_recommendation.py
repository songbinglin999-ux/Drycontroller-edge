"""
Rule-based diagnosis and suggestion service.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import time
from functools import lru_cache
from typing import Any, Dict, List, Optional

from app.services.realtime_data import (
    RealtimeDataService,
    get_realtime_data_service,
)


class ControlRecommendationService:
    SCHEMA_VERSION = "drycontroller.diagnosis_suggestion.v1"
    TARGET_MOISTURE = 12.0
    CURRENT_TAGS = [f"I_M{index}" for index in range(1, 16)]
    MOTOR_TAGS = [f"M{index}" for index in range(1, 16)]
    CONTEXT_TAGS = [
        "FTT1",
        "FTT2",
        "FTT3",
        "TT1",
        "TT2",
        "MT1",
        "VFM1",
        "VFM2",
        "VFM3",
        "VF1",
        "VF2",
        "VF3",
        "LL1",
        "LL2",
        "LL3",
        "LL4",
        "LL5",
        "LL6",
        "MOISTURE.PREDICTED",
    ]
    CONTROL_SOFTPOINTS: List[str] = []
    BLOCKED_BY = [
        "P0-SAFETY",
        "P0-CTRL-NAK",
        "P0-CTRL-MODE",
        "CONTROL_SOFTPOINTS_TBD",
    ]
    HISTORY_WINDOW = "30s"

    def __init__(self, realtime_service: Optional[RealtimeDataService] = None) -> None:
        self._realtime = realtime_service or get_realtime_data_service()

    async def get_recommendations(
        self,
        line: Optional[str],
        lookback: str = "5m",
    ) -> dict:
        payload = await self._realtime.get_latest_payload(
            requested_tags=self._diagnosis_tags(),
            lookback=lookback,
        )
        latest = self._to_map(payload["data"])
        analyses = await self._analyze_current_trends(latest, lookback)
        diagnosis = self._build_diagnosis(analyses, latest, lookback)
        suggestions = self._build_suggestions(diagnosis)
        return {
            "success": True,
            "data": {
                "schema_version": self.SCHEMA_VERSION,
                "line": line or "L1",
                "actionable": False,
                "actionability": "not_actionable",
                "blocked_by": list(self.BLOCKED_BY),
                "freshness": self._freshness(payload),
                "diagnosis": diagnosis,
                "suggestions": suggestions,
                "softsensor": {
                    "moisture_predicted": latest.get("MOISTURE.PREDICTED"),
                    "mt1": latest.get("MT1"),
                    "target_moisture": self.TARGET_MOISTURE,
                },
                "timestamp": int(time.time()),
            },
        }

    @staticmethod
    def _to_map(records: List[dict]) -> Dict[str, float]:
        result: Dict[str, float] = {}
        for record in records:
            tag = record.get("tag")
            value = record.get("value")
            if tag is not None and value is not None:
                result[tag] = value
        return result

    @classmethod
    def _diagnosis_tags(cls) -> List[str]:
        return cls.CURRENT_TAGS + cls.MOTOR_TAGS + cls.CONTEXT_TAGS

    async def _analyze_current_trends(self, latest: Dict[str, float], lookback: str) -> List[Dict[str, Any]]:
        histories = await asyncio.gather(
            *[
                self._realtime.get_history_payload(
                    tag=tag,
                    period=lookback,
                    window=self.HISTORY_WINDOW,
                    aggregation="mean",
                )
                for tag in self.CURRENT_TAGS
            ]
        )
        return [
            self._analyze_current_tag(
                tag=tag,
                current=latest.get(tag),
                motor_running=self._as_bool(latest.get(tag.replace("I_", ""))),
                points=history.get("data", {}).get("points", []),
            )
            for tag, history in zip(self.CURRENT_TAGS, histories)
        ]

    def _analyze_current_tag(
        self,
        tag: str,
        current: Optional[float],
        motor_running: Optional[bool],
        points: List[dict],
    ) -> Dict[str, Any]:
        values = [float(point["value"]) for point in points if point.get("value") is not None]
        baseline_values = values[:-1] if len(values) > 1 else values
        baseline = sum(baseline_values) / len(baseline_values) if baseline_values else None
        last_value = float(current) if current is not None else (values[-1] if values else None)
        first_value = values[0] if values else None
        previous_value = values[-2] if len(values) > 1 else None
        delta = (last_value - first_value) if last_value is not None and first_value is not None else None
        slope_per_min = self._slope_per_min(points, last_value)

        state = "missing"
        score = 0
        if last_value is not None:
            state = "normal"
            if baseline is not None and baseline > 0 and last_value >= baseline * 1.25:
                state = "sustained_high_load"
                score += 2
            if delta is not None and baseline is not None and delta >= max(1.0, abs(baseline) * 0.12):
                state = "rising_load"
                score += 2
            if previous_value is not None and baseline is not None and last_value - previous_value >= max(1.0, abs(baseline) * 0.15):
                state = "sudden_rise"
                score += 3
            if motor_running is False and baseline is not None and last_value >= max(1.0, abs(baseline) * 0.5):
                state = "current_with_motor_feedback_off"
                score += 3

        return {
            "tag": tag,
            "state": state,
            "score": score,
            "current": self._round(last_value),
            "baseline": self._round(baseline),
            "delta": self._round(delta),
            "slope_per_min": self._round(slope_per_min),
            "motor_running": motor_running,
        }

    def _build_diagnosis(
        self,
        analyses: List[Dict[str, Any]],
        latest: Dict[str, float],
        lookback: str,
    ) -> List[Dict[str, Any]]:
        abnormal = [item for item in analyses if item["score"] > 0]
        observed = [
            item
            for item in analyses
            if item["current"] is not None or item["baseline"] is not None
        ]
        evidence = sorted(abnormal or observed, key=lambda item: item["score"], reverse=True)[:5]
        warning_score = sum(item["score"] for item in abnormal)
        severity = "warning" if warning_score >= 3 else "info"
        probable_cause = (
            "load_rising_or_possible_downstream_blockage"
            if abnormal
            else "no_abnormal_load_trend_detected"
        )
        confidence = min(0.9, 0.55 + warning_score * 0.05) if abnormal else 0.55

        actions = [
            {
                "type": "inspect",
                "target": "downstream_material_path",
                "text": "Check downstream material flow and blockage risk before changing any setpoint.",
            },
            {
                "type": "observe",
                "target": "current_trend",
                "text": "Continue observing I_M current trend over the next 30s/5m window.",
            },
        ]
        if not abnormal:
            actions = [
                {
                    "type": "observe",
                    "target": "line_load",
                    "text": "Keep monitoring load trend; no control action is suggested.",
                }
            ]

        return [
            {
                "id": "load_trend",
                "severity": severity,
                "probable_cause": probable_cause,
                "confidence": self._round(confidence),
                "freshness": "fresh" if evidence else "missing",
                "evidence_window": {
                    "lookback": lookback,
                    "aggregation_window": self.HISTORY_WINDOW,
                },
                "evidence": evidence,
                "recommended_action": actions,
                "actionable": False,
                "blocked_by": list(self.BLOCKED_BY),
                "context": {
                    "moisture_predicted": latest.get("MOISTURE.PREDICTED"),
                    "mt1": latest.get("MT1"),
                },
            }
        ]

    def _build_suggestions(self, diagnosis: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "id": f"{item['id']}_suggestion",
                "diagnosis_id": item["id"],
                "severity": item["severity"],
                "confidence": item["confidence"],
                "recommended_action": item["recommended_action"],
                "actionable": False,
                "blocked_by": list(self.BLOCKED_BY),
                "freshness": item["freshness"],
            }
            for item in diagnosis
        ]

    @staticmethod
    def _freshness(payload: dict) -> str:
        if not payload.get("data"):
            return "missing"
        return "partial" if payload.get("missing") else "fresh"

    @staticmethod
    def _as_bool(value: Optional[float]) -> Optional[bool]:
        if value is None:
            return None
        return bool(value)

    @staticmethod
    def _slope_per_min(points: List[dict], current: Optional[float]) -> Optional[float]:
        if len(points) < 2 or current is None:
            return None
        first = points[0]
        first_time = ControlRecommendationService._parse_time(first.get("time"))
        last_time = ControlRecommendationService._parse_time(points[-1].get("time"))
        if not first_time or not last_time:
            return None
        elapsed = max(1.0, last_time - first_time)
        first_value = first.get("value")
        if first_value is None:
            return None
        return (float(current) - float(first_value)) / elapsed * 60.0

    @staticmethod
    def _parse_time(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        try:
            normalized = value.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            return None

    @staticmethod
    def _round(value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        return round(float(value), 3)


@lru_cache(maxsize=1)
def get_control_recommendation_service() -> ControlRecommendationService:
    return ControlRecommendationService()
