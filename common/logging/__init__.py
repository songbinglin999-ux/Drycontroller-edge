"""
通用日志与可观测性工具。
"""

from .bootstrap import setup_logging, get_event_recorder  # noqa: F401
from .events import EventRecorder  # noqa: F401

__all__ = ["setup_logging", "get_event_recorder", "EventRecorder"]


