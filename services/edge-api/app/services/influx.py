"""
InfluxDB 查询服务
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from functools import lru_cache
from typing import Any, Dict, List

from influxdb_client import InfluxDBClient

from app.core.config import settings

DURATION_PATTERN = re.compile(r"^\d+(s|m|h|d|w)$")
VALID_AGGREGATIONS = {"mean", "max", "min", "sum", "last", "first"}


class InfluxQueryService:
    def __init__(self) -> None:
        self.client = InfluxDBClient(
            url=settings.influxdb_url,
            token=settings.INFLUXDB_TOKEN,
            org=settings.INFLUXDB_ORG,
            timeout=60_000,
        )
        self.query_api = self.client.query_api()
        self.bucket = settings.INFLUXDB_BUCKET
        self.measurement = os.getenv("INFLUXDB_MEASUREMENT", "industrial_data")

    async def latest_points(self, tags: List[str], lookback: str = "5m") -> List[Dict[str, Any]]:
        if not tags:
            return []
        lookback = self._validate_duration(lookback)
        flux = self._build_latest_flux(tags, lookback)
        return await asyncio.to_thread(self._query_and_parse_latest, flux)

    async def history_points(
        self,
        tag: str,
        period: str = "1h",
        window: str = "1m",
        aggregation: str = "mean",
    ) -> List[Dict[str, Any]]:
        period = self._validate_duration(period)
        window = self._validate_duration(window)
        aggregation = self._validate_aggregation(aggregation)
        flux = self._build_history_flux(tag, period, window, aggregation)
        return await asyncio.to_thread(self._query_and_parse_history, flux)

    def _build_latest_flux(self, tags: List[str], lookback: str) -> str:
        tag_list = json.dumps(tags)
        return f"""
tagList = {tag_list}
from(bucket: "{self.bucket}")
  |> range(start: -{lookback})
  |> filter(fn: (r) => r["_measurement"] == "{self.measurement}")
  |> filter(fn: (r) => contains(value: r["tag"], set: tagList))
  |> pivot(rowKey: ["_time", "tag"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "tag", "value", "unit"])
  |> sort(columns: ["_time"], desc: true)
  |> unique(column: "tag")
"""

    def _build_history_flux(self, tag: str, period: str, window: str, aggregation: str) -> str:
        return f"""
from(bucket: "{self.bucket}")
  |> range(start: -{period})
  |> filter(fn: (r) => r["_measurement"] == "{self.measurement}")
  |> filter(fn: (r) => r["tag"] == "{tag}")
  |> filter(fn: (r) => r["_field"] == "value")
  |> aggregateWindow(every: {window}, fn: {aggregation}, createEmpty: false)
  |> keep(columns: ["_time", "_value", "tag"])
  |> sort(columns: ["_time"], desc: false)
"""

    def _query_and_parse_latest(self, flux: str) -> List[Dict[str, Any]]:
        tables = self.query_api.query(flux)
        result: List[Dict[str, Any]] = []
        for table in tables:
            for record in table.records:
                values = record.values
                result.append(
                    {
                        "tag": values.get("tag"),
                        "value": values.get("value"),
                        "unit": values.get("unit", ""),
                        "time": values.get("_time").isoformat() if values.get("_time") else None,
                    }
                )
        return result

    def _query_and_parse_history(self, flux: str) -> List[Dict[str, Any]]:
        tables = self.query_api.query(flux)
        result: List[Dict[str, Any]] = []
        for table in tables:
            for record in table.records:
                values = record.values
                result.append(
                    {
                        "time": values.get("_time").isoformat() if values.get("_time") else None,
                        "value": values.get("_value"),
                    }
                )
        return result

    @staticmethod
    def _validate_duration(value: str) -> str:
        if not DURATION_PATTERN.match(value):
            raise ValueError("Invalid duration format. Use e.g. 5m, 1h, 1d.")
        return value

    @staticmethod
    def _validate_aggregation(value: str) -> str:
        value_lower = value.lower()
        if value_lower not in VALID_AGGREGATIONS:
            raise ValueError(f"Unsupported aggregation '{value}'.")
        return value_lower


@lru_cache(maxsize=1)
def get_influx_service() -> InfluxQueryService:
    return InfluxQueryService()

