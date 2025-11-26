"""
Edge API 日志封装。
"""

from common.logging import setup_logging as _setup_logging, get_event_recorder

__all__ = ["setup_logging", "get_event_recorder"]


def setup_logging():
    """
    对 Edge API 启用统一日志配置。
    """
    return _setup_logging("edge-api")

