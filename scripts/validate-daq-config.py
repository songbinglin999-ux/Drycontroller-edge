#!/usr/bin/env python3
"""
对 edge-daq 关键配置做静态校验，在 CI/提交前发现问题：
- configs/tags.yaml：必填字段、信号类型、类型、范围、报警阈值
- acquisition/heartbeat 节点的基础约束

用法：
    python scripts/validate-daq-config.py --file configs/tags.yaml

退出码：
- 0：全部通过
- 1：校验失败
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover - 环境缺失提示
    print("需要 PyYAML，请先安装：pip install pyyaml", file=sys.stderr)
    raise SystemExit(1) from exc


def _validate_acquisition(cfg: Dict[str, Any], errors: List[str]) -> None:
    acquisition = cfg.get("acquisition", {}) or {}
    scan = acquisition.get("scan_interval")
    if scan is not None:
        try:
            if float(scan) <= 0:
                errors.append("acquisition.scan_interval must be > 0")
        except (TypeError, ValueError):
            errors.append("acquisition.scan_interval must be numeric")
    batch = acquisition.get("batch_size")
    if batch is not None:
        try:
            if int(batch) < 0:
                errors.append("acquisition.batch_size must be >= 0")
        except (TypeError, ValueError):
            errors.append("acquisition.batch_size must be integer")


def _validate_heartbeat(cfg: Dict[str, Any], errors: List[str]) -> None:
    hb = cfg.get("heartbeat", {}) or {}
    if not hb:
        return
    fc = int(hb.get("function_code", 3) or 3)
    if fc not in (3, 4, 16):
        errors.append("heartbeat.function_code must be 3/4/16")
    for field in ("read_address", "write_address"):
        if hb.get(field) is not None:
            try:
                int(hb.get(field))
            except (TypeError, ValueError):
                errors.append(f"heartbeat.{field} must be integer")


def _validate_tags(cfg: Dict[str, Any], errors: List[str]) -> None:
    tags_cfg = (cfg or {}).get("tags", {})
    allowed_types = {"float", "int", "int32", "uint32", "bool"}
    allowed_signal_types = {"DI", "AI", "DO", "AO"}

    def iter_tags(node: Any):
        if isinstance(node, dict):
            for v in node.values():
                yield from iter_tags(v)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, dict):
                    yield item

    for tag in iter_tags(tags_cfg):
        name = tag.get("name")
        t = tag.get("type")
        signal_type = tag.get("signal_type")
        if not name or not t or not signal_type:
            errors.append(f"{name or '<unknown>'}: missing name/type/signal_type")
            continue
        if signal_type not in allowed_signal_types:
            errors.append(f"{name}: invalid signal_type {signal_type}")
        rng = tag.get("range")
        if rng and (not isinstance(rng, (list, tuple)) or len(rng) != 2):
            errors.append(f"{name}: range must be [min,max]")
        alarm_high = tag.get("alarm_high")
        alarm_low = tag.get("alarm_low")
        if alarm_high is not None and alarm_low is not None and alarm_low > alarm_high:
            errors.append(f"{name}: alarm_low > alarm_high")
        if str(t).lower() not in allowed_types:
            errors.append(f"{name}: invalid type {t}")


def validate(config_path: Path) -> List[str]:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    errors: List[str] = []
    _validate_acquisition(cfg, errors)
    _validate_heartbeat(cfg, errors)
    _validate_tags(cfg, errors)
    return errors


def main():
    parser = argparse.ArgumentParser(description="校验 edge-daq 点表配置（tags.yaml）")
    parser.add_argument("--file", default="configs/tags.yaml", help="配置文件，默认 configs/tags.yaml")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"文件不存在: {path}", file=sys.stderr)
        sys.exit(1)

    errors = validate(path)
    if errors:
        print("❌ 配置校验失败：", file=sys.stderr)
        for err in errors:
            print(f" - {err}", file=sys.stderr)
        sys.exit(1)
    print("✅ 配置校验通过")


if __name__ == "__main__":
    main()
