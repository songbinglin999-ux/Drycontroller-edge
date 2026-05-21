"""
Edge-api 本地持久化 ORM 模型（P1-5 phase B / edge）。

设计原则（综合 audit P1-5 review）：
- tenant_id 保留但 nullable——edge 通常单租户，云端回收时仍可分租。
- node 身份用 node_id/node_name/device_id 三个字符串字段，不做 FK——
  断网/未 enroll 时也能落库。
- 时序数据走 InfluxDB；这里只放"带审计语义的事实记录"。
- 状态机：control_command_history / ota_operations / cloud_sync_outbox
  有明确 status；其他表 append-only 或由时间字段推导。
"""

from __future__ import annotations

# pyright: reportMissingImports=false

import datetime as dt
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.sqlite import JSON as SQLITE_JSON
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# JSON 字段在 postgres 用 JSONB，sqlite 用 JSON；MutableDict 让 in-place 修改可落库。
_JSON_BASE = JSON().with_variant(SQLITE_JSON, "sqlite").with_variant(JSONB, "postgresql")
JSON_DICT = MutableDict.as_mutable(_JSON_BASE)


class AlarmEvent(Base):
    """报警事件：原始 raise / ack / silence / resolve 的完整轨迹。"""

    __tablename__ = "alarm_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    line_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    node_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    node_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    device_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    severity: Mapped[str] = mapped_column(String(16), nullable=False)  # info / warning / critical
    source_service: Mapped[str] = mapped_column(String(64), nullable=False)
    alarm_key: Mapped[str] = mapped_column(String(128), nullable=False)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Optional cache，避免每次都靠时间字段推导。可为空。
    state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    raised_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)

    ack_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ack_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    silenced_until: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    meta: Mapped[dict[str, Any]] = mapped_column(JSON_DICT, default=dict)

    __table_args__ = (
        Index("ix_alarm_events_tenant_raised", "tenant_id", "raised_at"),
        Index("ix_alarm_events_alarm_key_raised", "alarm_key", "raised_at"),
    )


class OperationAuditLog(Base):
    """运维操作审计：append-only，谁在什么时候对什么做了什么。"""

    __tablename__ = "operation_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    actor: Mapped[str] = mapped_column(String(128), nullable=False)  # user_id / system / cloud-ops-job-N
    action: Mapped[str] = mapped_column(String(128), nullable=False)  # ota.start / control.write / license.install ...
    resource_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    correlation_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    cloud_job_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    result: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # success / failure / accepted
    error_msg: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    detail: Mapped[dict[str, Any]] = mapped_column(JSON_DICT, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_audit_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_action_created", "action", "created_at"),
    )


class ControlCommandHistory(Base):
    """控制命令历史：谁/云端哪个 job/什么时候/写了哪个 tag/ACK 是否成功。"""

    __tablename__ = "control_command_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    line_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    cloud_job_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    node_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    command_type: Mapped[str] = mapped_column(String(32), nullable=False)  # set / batch / emergency_stop
    tag: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    correlation_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON_DICT, default=dict)

    requested_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    requested_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    ack_status: Mapped[str] = mapped_column(String(16), server_default="pending", nullable=False)  # pending/ok/error/timeout
    ack_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ack_detail: Mapped[dict[str, Any]] = mapped_column(JSON_DICT, default=dict)
    error_msg: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_control_job", "cloud_job_id"),
        Index("ix_control_requested", "requested_at"),
        Index("ix_control_ack_status", "ack_status"),
    )


class OtaOperation(Base):
    """OTA 操作记录：版本 / source / 状态机 / 日志摘要。"""

    __tablename__ = "ota_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    operation_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    ota_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # online / offline / offline-tar
    status: Mapped[str] = mapped_column(String(32), server_default="pending", nullable=False)
    # pending / applying / done / rolled_back / rollback_failed / failed

    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rollback_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    params: Mapped[dict[str, Any]] = mapped_column(JSON_DICT, default=dict)
    log_excerpt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    log_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON_DICT, default=dict)

    __table_args__ = (
        Index("ix_ota_operation_id", "operation_id"),
        Index("ix_ota_started", "started_at"),
        Index("ix_ota_status", "status"),
    )


class PreflightRun(Base):
    """出厂 preflight 检查结果。"""

    __tablename__ = "preflight_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # pass / degraded / fail——boolean 不够表达 degraded
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)  # helper：status == "pass"
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    json_output: Mapped[dict[str, Any]] = mapped_column(JSON_DICT, default=dict)

    __table_args__ = (
        Index("ix_preflight_run_at", "run_at"),
    )


class ConfigVersion(Base):
    """配置版本：plc/monitor/daq 等配置的每一次变更。"""

    __tablename__ = "config_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)  # plc / monitor / daq / ai-* / ...
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    checksum: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # local / cloud / factory
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DICT, default=dict)

    active: Mapped[bool] = mapped_column(Boolean, server_default="0", nullable=False)
    previous_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    applied_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    applied_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    restart_required: Mapped[bool] = mapped_column(Boolean, server_default="0", nullable=False)
    rolled_back_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_config_scope_applied", "scope", "applied_at"),
    )


class CloudSyncOutbox(Base):
    """云端断网时的上报 outbox：edge-status / audit / event 等。"""

    __tablename__ = "cloud_sync_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)  # edge-status / audit / alarm / control_command / ...
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DICT, default=dict)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    max_attempts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # NULL = 永久重试
    next_retry_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # pending / synced / dead——可由 synced_at + max_attempts 推导，加字段是为查询简单
    status: Mapped[str] = mapped_column(String(16), server_default="pending", nullable=False)
    synced_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_outbox_pending_next_retry", "synced_at", "next_retry_at", "created_at"),
        Index("ix_outbox_kind_created", "kind", "created_at"),
    )


__all__ = [
    "AlarmEvent",
    "OperationAuditLog",
    "ControlCommandHistory",
    "OtaOperation",
    "PreflightRun",
    "ConfigVersion",
    "CloudSyncOutbox",
]
