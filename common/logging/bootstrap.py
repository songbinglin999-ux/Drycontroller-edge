"""
统一 structlog 配置与日志落盘。
"""

from __future__ import annotations

import json
import logging
import logging.config
from logging.handlers import TimedRotatingFileHandler
import os
from pathlib import Path
import threading
from typing import Optional

import structlog

from common.logging.events import EventRecorder

_LOCK = threading.Lock()
_CONFIGURED = False
_EVENT_RECORDER: EventRecorder | None = None


def setup_logging(
    service_name: str,
    *,
    log_level: Optional[str] = None,
    log_root: Optional[str | Path] = None,
) -> EventRecorder:
    """
    配置结构化日志，并返回事件记录器。
    """
    global _CONFIGURED, _EVENT_RECORDER

    with _LOCK:
        if _CONFIGURED and _EVENT_RECORDER:
            return _EVENT_RECORDER

        level_name = (log_level or os.getenv("LOG_LEVEL", "INFO")).upper()
        level_value = getattr(logging, level_name, logging.INFO)

        log_dir = _ensure_log_dir(service_name, log_root)
        log_file = log_dir / "app.log"

        logging.config.dictConfig(
            {
                "version": 1,
                "disable_existing_loggers": False,
                "formatters": {
                    "plain": {
                        "format": "%(message)s",
                    }
                },
                "handlers": {
                    "console": {
                        "class": "logging.StreamHandler",
                        "formatter": "plain",
                        "level": level_value,
                        "stream": "ext://sys.stdout",
                    },
                    "file": {
                        "()": TimedRotatingFileHandler,
                        "formatter": "plain",
                        "level": level_value,
                        "filename": str(log_file),
                        "when": os.getenv("LOG_ROTATE_WHEN", "midnight"),
                        "backupCount": int(os.getenv("LOG_BACKUP_COUNT", "7")),
                        "encoding": "utf-8",
                    },
                },
                "root": {
                    "level": level_value,
                    "handlers": ["console", "file"],
                },
            }
        )

        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                _add_static_context(service_name),
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.EventRenamer("message"),
                structlog.processors.UnicodeDecoder(),
                structlog.processors.JSONRenderer(serializer=_json_serializer),
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )

        _EVENT_RECORDER = EventRecorder(service_name=service_name)
        _CONFIGURED = True
        return _EVENT_RECORDER


def get_event_recorder() -> EventRecorder:
    """
    获取事件记录器。
    """
    if not _EVENT_RECORDER:
        raise RuntimeError("setup_logging 必须在调用 get_event_recorder 之前执行")
    return _EVENT_RECORDER


def _add_static_context(service_name: str):
    env_name = (
        os.getenv("APP_ENV")
        or os.getenv("ENVIRONMENT")
        or os.getenv("ENV")
        or "production"
    )
    instance_id = os.getenv("INSTANCE_ID") or os.getenv("HOSTNAME")

    def processor(logger, _method_name, event_dict):
        event_dict.setdefault("service", service_name)
        event_dict.setdefault("env", env_name)
        if instance_id:
            event_dict.setdefault("instance", instance_id)
        if "module" not in event_dict:
            logger_name = getattr(logger, "name", None)
            if logger_name:
                event_dict["module"] = logger_name
        return event_dict

    return processor


def _ensure_log_dir(service_name: str, log_root: Optional[str | Path]) -> Path:
    root_candidate = Path(
        log_root or os.getenv("LOG_ROOT", "/logs")
    )
    if not root_candidate.is_absolute():
        root_candidate = (Path.cwd() / root_candidate).resolve()

    log_root_path = _mkdir_with_fallback(root_candidate)
    service_dir = log_root_path / service_name
    service_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("SERVICE_LOG_DIR", str(service_dir))
    return service_dir


def _mkdir_with_fallback(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except PermissionError:
        fallback = (Path.cwd() / "logs").resolve()
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _json_serializer(event_dict, *, default=None, **kwargs):
    return json.dumps(
        event_dict,
        ensure_ascii=False,
        separators=(",", ":"),
        default=default,
        **kwargs,
    )


