"""
标签配置加载工具
"""

from functools import lru_cache
from typing import Dict, List

import yaml

from app.core.config import settings


def _walk_tags(node, collector: List[str]):
    if isinstance(node, dict):
        if "name" in node:
            collector.append(node["name"])
        for value in node.values():
            _walk_tags(value, collector)
    elif isinstance(node, list):
        for item in node:
            _walk_tags(item, collector)


@lru_cache(maxsize=1)
def get_all_tag_names() -> List[str]:
    """
    从 tags.yaml 中获取所有 tag name 列表
    """
    try:
        with open(settings.TAGS_CONFIG, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return []

    tags_section = config.get("tags", {})
    collector: List[str] = []
    _walk_tags(tags_section, collector)
    # 去重并保持稳定排序
    unique = sorted(set(filter(None, collector)))
    return unique


@lru_cache(maxsize=1)
def get_tag_config() -> Dict:
    try:
        with open(settings.TAGS_CONFIG, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}

