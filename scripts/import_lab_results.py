#!/usr/bin/env python3
"""
从 CSV 导入实验室含水率数据并写入 softsensor_training_data measurement。

CSV 要求至少包含列：
    time (ISO8601 或 epoch 秒/毫秒)
    line_id
    moisture_lab

可选列：
    lab_sample_id, lab_operator, feature_window_start, feature_window_end, notes 等

示例：
    python scripts/import_lab_results.py lab_results.csv --line L1
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from influxdb_client import InfluxDBClient, Point

from app.core.config import settings


def parse_timestamp(value: str) -> datetime:
    value = value.strip()
    if value.isdigit():
        # epoch 秒或毫秒
        ts = int(value)
        if ts > 10**12:
            ts = ts / 1000.0
        return datetime.utcfromtimestamp(ts)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build_point(row: Dict[str, str]) -> Point:
    ts = parse_timestamp(row["time"])
    point = Point(settings.TRAINING_MEASUREMENT)
    point.tag("line_id", row.get("line_id", "L1"))
    if row.get("lab_sample_id"):
        point.tag("lab_sample_id", row["lab_sample_id"])
    if row.get("lab_operator"):
        point.tag("lab_operator", row["lab_operator"])

    point.field("moisture_lab", float(row["moisture_lab"]))

    optional_fields = [
        "moisture_predicted",
        "feature_window_start",
        "feature_window_end",
        "notes",
    ]
    for field in optional_fields:
        if row.get(field):
            try:
                value = float(row[field])
            except ValueError:
                value = row[field]
            point.field(field, value)

    point.time(ts)
    return point


def load_csv(path: Path, line_override: Optional[str]) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        rows = []
        for row in reader:
            if line_override:
                row["line_id"] = line_override
            rows.append(row)
        return rows


def write_points(rows: List[Dict[str, str]]):
    client = InfluxDBClient(
        url=settings.influxdb_url,
        token=settings.INFLUXDB_TOKEN,
        org=settings.INFLUXDB_ORG,
    )
    write_api = client.write_api()
    points = [build_point(row) for row in rows]
    write_api.write(bucket=settings.INFLUXDB_BUCKET, record=points)
    client.close()


def main():
    parser = argparse.ArgumentParser(description="导入实验室含水率 CSV 数据")
    parser.add_argument("csv_path", type=Path, help="实验室 CSV 文件路径")
    parser.add_argument("--line", default=None, help="覆盖 CSV 中的 line_id")
    args = parser.parse_args()

    rows = load_csv(args.csv_path, args.line)
    write_points(rows)
    print(f"Imported {len(rows)} rows into measurement '{settings.TRAINING_MEASUREMENT}'")


if __name__ == "__main__":
    main()

