#!/usr/bin/env python3
"""
导出 softsensor_training_data 到 CSV/JSON

示例：
    python scripts/export_training_dataset.py --start -7d --line L1 --format csv --output L1_week.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from typing import List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from app.services.influx import InfluxQueryService  # noqa: E402
from app.core.config import settings  # noqa: E402


async def export_training_samples(args) -> List[dict]:
    influx = InfluxQueryService()
    records = await influx.training_samples(
        measurement=settings.TRAINING_MEASUREMENT,
        start=args.start,
        stop=args.stop,
        line=args.line,
        require_lab=args.require_lab,
    )
    if args.limit:
        records = records[: args.limit]
    return records


def write_csv(records: List[dict], output: Path):
    if not records:
        output.write_text("", encoding="utf-8")
        return
    fieldnames = sorted(records[0].keys())
    with output.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def write_json(records: List[dict], output: Path):
    with output.open("w", encoding="utf-8") as fp:
        json.dump(records, fp, ensure_ascii=False, indent=2)


async def main():
    parser = argparse.ArgumentParser(description="导出 softsensor 训练数据")
    parser.add_argument("--start", default="-7d", help="Flux range start，例：-7d")
    parser.add_argument("--stop", default="now()", help="Flux range stop，默认 now()")
    parser.add_argument("--line", default=None, help="可选：按产线过滤，例如 L1")
    parser.add_argument(
        "--require-lab",
        action="store_true",
        default=False,
        help="仅导出已有 LAB 的样本",
    )
    parser.add_argument("--limit", type=int, default=0, help="最多导出的记录数（0 表示全部）")
    parser.add_argument(
        "--format",
        choices=["json", "csv"],
        default="json",
        help="输出格式",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("training_samples.json"),
        help="输出文件路径",
    )
    args = parser.parse_args()

    records = await export_training_samples(args)
    if args.format == "csv":
        write_csv(records, args.output)
    else:
        write_json(records, args.output)

    print(f"Exported {len(records)} records to {args.output}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())

