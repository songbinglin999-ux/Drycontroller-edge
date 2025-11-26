#!/usr/bin/env python3
"""
校验 configs/tags.yaml 中的 Tag 命名是否符合约定：
- EQUIPMENT 必须在 allowed_equipment 列表中
- 至少包含 2 个段（EQUIPMENT + METRIC），最多 4 个段
- SECTION（若存在）需落在 section_alias 列表中
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover - 环境缺失提示
    print("需要 PyYAML 才能解析 configs/tags.yaml，请先运行 `pip install pyyaml`。", file=sys.stderr)
    raise SystemExit(1) from exc

RE_TAG = re.compile(r"^[A-Z0-9]+(?:\.[A-Z0-9]+){1,3}$")


def iter_tags(node: Any) -> Iterable[str]:
    if isinstance(node, dict):
        name = node.get("name")
        if isinstance(name, str):
            yield name
        for value in node.values():
            yield from iter_tags(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_tags(item)


def validate_tags(config: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    naming = config.get("naming", {})
    allowed_equipment = set(naming.get("equipment", {}).keys())
    allowed_sections = set(naming.get("section_alias", {}).keys())

    tags_root = config.get("tags", {})
    for tag in iter_tags(tags_root):
        if not RE_TAG.match(tag):
            errors.append(f"{tag}: 不符合格式 {RE_TAG.pattern}")
            continue
        parts = tag.split(".")
        equipment = parts[0]
        if allowed_equipment and equipment not in allowed_equipment:
            errors.append(f"{tag}: EQUIPMENT '{equipment}' 不在 allowed 列表中")
        if len(parts) < 2:
            errors.append(f"{tag}: 至少需要 EQUIPMENT + METRIC 两段")
            continue
        if len(parts) > 4:
            errors.append(f"{tag}: 不应超过 4 段")
        sections = parts[1:-1]
        for section in sections:
            if allowed_sections and section not in allowed_sections:
                errors.append(f"{tag}: SECTION '{section}' 不在 section_alias 列表中")
    return errors


def main():
    parser = argparse.ArgumentParser(description="校验 Tag 命名")
    parser.add_argument(
        "--file",
        default="configs/tags.yaml",
        help="标签配置文件路径（默认 configs/tags.yaml）",
    )
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"文件不存在: {path}", file=sys.stderr)
        sys.exit(1)

    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    errors = validate_tags(config)
    if errors:
        print("Tag 命名校验失败：", file=sys.stderr)
        for err in errors:
            print(f" - {err}", file=sys.stderr)
        sys.exit(1)
    print("✅ Tag 命名全部符合约定")


if __name__ == "__main__":
    main()

