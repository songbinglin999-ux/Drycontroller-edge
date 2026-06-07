"""
标签配置加载工具
"""

from functools import lru_cache
from typing import Any, Dict, List

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


def _load_tag_config() -> Dict:
    try:
        with open(settings.TAGS_CONFIG, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _walk_tag_metadata(node, collector: List[Dict[str, Any]]) -> None:
    if isinstance(node, dict):
        name = node.get("name")
        if isinstance(name, str) and name:
            collector.append(
                {
                    "name": name,
                    "signal_type": str(node.get("signal_type", "")),
                    "type": str(node.get("type", "")),
                    "unit": str(node.get("unit", "")),
                    "description": str(node.get("description", "")),
                }
            )
        for value in node.values():
            _walk_tag_metadata(value, collector)
    elif isinstance(node, list):
        for item in node:
            _walk_tag_metadata(item, collector)


@lru_cache(maxsize=1)
def get_all_tag_names() -> List[str]:
    """
    从 tags.yaml 中获取所有 tag name 列表
    """
    config = _load_tag_config()
    tags_section = config.get("tags", {})
    collector: List[str] = []
    _walk_tags(tags_section, collector)
    # 去重并保持稳定排序
    unique = sorted(set(filter(None, collector)))
    return unique


@lru_cache(maxsize=1)
def get_all_tag_metadata() -> List[Dict[str, Any]]:
    """
    从 tags.yaml 中获取全量 tag 元数据，供云端实时窗口展示位号说明与单位。
    """
    config = _load_tag_config()
    tags_section = config.get("tags", {})
    collector: List[Dict[str, Any]] = []
    _walk_tag_metadata(tags_section, collector)

    by_name = {item["name"]: item for item in collector}
    return [by_name[name] for name in sorted(by_name)]


@lru_cache(maxsize=1)
def get_tag_config() -> Dict:
    return _load_tag_config()
