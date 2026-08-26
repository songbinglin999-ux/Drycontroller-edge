"""
InfluxQueryService 参数校验与解析单测

覆盖：
- duration 格式校验
- aggregation 白名单
- flux table 解析：过滤内部字段、time isoformat
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]
for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

from app.services.influx import InfluxQueryService  # noqa: E402


@pytest.mark.parametrize("value", ["5m", "1h", "1d", "30s", "2w"])
def test_validate_duration_ok(value: str):
    assert InfluxQueryService._validate_duration(value) == value


@pytest.mark.parametrize("value", ["", "5", "m5", "1M", "1y", "1h ", " 1h", "1hour"])
def test_validate_duration_bad(value: str):
    with pytest.raises(ValueError):
        InfluxQueryService._validate_duration(value)


@pytest.mark.parametrize("value,expected", [("mean", "mean"), ("MAX", "max"), ("Last", "last")])
def test_validate_aggregation_ok(value: str, expected: str):
    assert InfluxQueryService._validate_aggregation(value) == expected


def test_validate_aggregation_rejects_unknown():
    with pytest.raises(ValueError):
        InfluxQueryService._validate_aggregation("median")


@pytest.mark.parametrize(
    "value",
    [
        "DRYER1.OUT.TEMP",
        "softsensor_training_data",
        "industrial-data",
        "edge-api:metrics",
        "L1",
        "degraded",
    ],
)
def test_validate_identifier_ok(value: str):
    assert InfluxQueryService._validate_identifier(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "DRYER1.OUT.TEMP\"",
        "temp) |> drop()",
        "system\nhealth",
        "field value",
        "../../../secret",
    ],
)
def test_validate_identifier_bad(value: str):
    with pytest.raises(ValueError):
        InfluxQueryService._validate_identifier(value)


@pytest.mark.parametrize(
    "value",
    ["-7d", "-12h", "now()", "2026-05-19T01:02:03Z", "2026-05-19T01:02:03.123Z"],
)
def test_validate_range_bound_ok(value: str):
    assert InfluxQueryService._validate_range_bound(value, "start") == value


@pytest.mark.parametrize("value", ["7d", "-7months", 'now() |> drop()', "2026-05-19", ""])
def test_validate_range_bound_bad(value: str):
    with pytest.raises(ValueError):
        InfluxQueryService._validate_range_bound(value, "start")


@pytest.mark.asyncio
async def test_public_query_builders_reject_injected_user_values():
    svc = InfluxQueryService.__new__(InfluxQueryService)
    svc.bucket = "industrial-data"
    svc.measurement = "industrial_data"

    with pytest.raises(ValueError):
        await svc.latest_points(['A.TAG") |> drop()'])
    with pytest.raises(ValueError):
        await svc.history_points('A.TAG") |> drop()')
    with pytest.raises(ValueError):
        await svc.training_samples("softsensor_training_data", '-7d) |> drop()')
    with pytest.raises(ValueError):
        await svc.system_metric_trend("system_metrics", 'cpu") |> drop()')
    with pytest.raises(ValueError):
        await svc.active_service_alarms("system_health", severity='down") |> drop()')


def test_parse_flux_tables_filters_internal_fields_and_adds_time():
    # 构造最小的 flux 返回结构：table.records[].values
    class DummyRecord:
        def __init__(self, values):
            self.values = values

    class DummyTable:
        def __init__(self, records):
            self.records = records

    dt = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    tables = [
        DummyTable(
            records=[
                DummyRecord(
                    {
                        "_time": dt,
                        "_start": "ignored",
                        "_stop": "ignored",
                        "_value": 1.23,
                        "tag": "A.TAG",
                        "value": 1.23,
                    }
                )
            ]
        )
    ]

    # 避免触发真实 InfluxDBClient 初始化：构造一个未执行 __init__ 的实例
    svc = InfluxQueryService.__new__(InfluxQueryService)
    rows = svc._tables_to_dicts(tables)
    assert len(rows) == 1
    row = rows[0]
    assert row["tag"] == "A.TAG"
    assert row["value"] == 1.23
    assert row["time"] == dt.isoformat()
    # _time 允许保留；其他 _ 开头字段应过滤
    assert "_time" in row
    assert "_start" not in row
    assert "_stop" not in row
    assert "_value" not in row


def test_latest_points_preserve_quality_and_time():
    class DummyRecord:
        values = {
            "_time": datetime(2026, 8, 26, tzinfo=timezone.utc),
            "tag": "M1_I",
            "value": 10.1,
            "unit": "A",
            "quality": "good",
        }

    class DummyAPI:
        def query(self, flux):
            assert '"quality"' in flux
            return [type("Table", (), {"records": [DummyRecord()]})()]

    svc = InfluxQueryService.__new__(InfluxQueryService)
    svc.query_api = DummyAPI()
    rows = svc._query_and_parse_latest(
        'keep(columns: ["_time", "tag", "value", "unit", "quality"])'
    )
    assert rows == [
        {
            "tag": "M1_I",
            "value": 10.1,
            "unit": "A",
            "quality": "good",
            "time": "2026-08-26T00:00:00+00:00",
        }
    ]
