"""
系统监控服务

本模块实现边缘节点的系统监控功能，包括：
- 系统指标采集：CPU、内存、磁盘使用率
- 服务健康检查：HTTP/TCP/容器状态探活
- 状态发布：通过 MQTT 推送、写入 InfluxDB

监控架构：
┌────────────────┐    ┌─────────────────┐    ┌─────────────┐
│  系统指标采集   │ -> │                 │ -> │   MQTT      │
│  (psutil)      │    │ MonitorService  │    │  (实时推送)  │
└────────────────┘    │                 │    └─────────────┘
                      │                 │
┌────────────────┐    │                 │    ┌─────────────┐
│  服务健康检查   │ -> │                 │ -> │  InfluxDB   │
│  (HTTP/TCP/容器)│    └─────────────────┘    │  (历史存储)  │
└────────────────┘                            └─────────────┘

状态级别优先级：
- unknown(0) < healthy(1) < degraded(2) < down(3)
"""

import asyncio
from collections import deque
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import docker
from docker import errors as docker_errors
import httpx
import psutil
import structlog
import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.security import APIKeyHeader
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import ASYNCHRONOUS
from influxdb_client.rest import ApiException
import paho.mqtt.client as mqtt
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel

from common.logging import setup_logging
from monitor.audit import SelfHealAuditor
from monitor.discovery import DiscoveryAgent
from monitor.self_heal import (
    SelfHealDecision,
    SelfHealManager,
    SelfHealOutcome,
)

event_recorder = setup_logging("edge-monitor")
logger = structlog.get_logger(__name__)

# 监控配置文件路径
DEFAULT_CONFIG_PATH = Path(os.getenv("MONITOR_CONFIG", "/app/configs/monitor.yaml"))

# 状态优先级映射：数值越大表示问题越严重
STATUS_PRIORITY = {
    "unknown": 0,    # 未知状态
    "healthy": 1,    # 健康
    "degraded": 2,   # 降级（部分功能受影响）
    "down": 3,       # 宕机（完全不可用）
}


# health 状态 → alarm severity 映射
_STATUS_TO_SEVERITY = {
    "degraded": "warning",
    "down": "critical",
}


def _normalize_health_status(value: str | None) -> str:
    s = (value or "").lower()
    if s in ("ok", "healthy"):
        return "healthy"
    if s in ("degraded", "down"):
        return s
    # unknown / 其它：视同 healthy（不发 alarm；现有代码已将 unknown 默认升级为 degraded）
    return "healthy"


class SelfHealToggleRequest(BaseModel):
    service: str
    enabled: bool


class SelfHealRestartRequest(BaseModel):
    service: str


_ops_api_key_header_name = os.getenv("MONITOR_API_KEY_HEADER", "X-API-Key")
_ops_api_key_scheme = APIKeyHeader(name=_ops_api_key_header_name, auto_error=False)


def _split_api_keys(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _get_ops_api_keys() -> set[str]:
    # 专用 key 和 admin key 都允许；edge-api 代理 /system/container/logs 时会转发 admin key。
    keys = _split_api_keys(os.getenv("MONITOR_OPS_API_KEYS"))
    keys.extend(_split_api_keys(os.getenv("ADMIN_API_KEYS")))
    return set(keys)


async def require_ops_api_key(api_key: str | None = Depends(_ops_api_key_scheme)) -> str:
    """
    edge-monitor 的高危运维接口鉴权（/ops/*）。

    - 未配置密钥：返回 503（避免“默认裸奔”）
    - 缺失密钥：401
    - 密钥不匹配：403
    """
    allowed = _get_ops_api_keys()
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ops api key security is not configured (set MONITOR_OPS_API_KEYS or ADMIN_API_KEYS)",
        )
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
            headers={"WWW-Authenticate": "API-Key"},
        )
    if api_key not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key not permitted",
        )
    return api_key


class MonitorService:
    """
    系统监控服务
    
    负责边缘节点的系统指标采集、服务健康检查和状态发布。
    
    主要功能：
    - 周期性采集 CPU/内存/磁盘指标
    - 并行检查多个服务的健康状态（HTTP/TCP/容器）
    - 将监控数据写入 InfluxDB
    - 通过 MQTT 发布实时状态
    
    Attributes:
        system_metrics: 最新的系统指标数据
        service_status: 各服务的健康状态
        system_interval: 系统指标采集间隔（秒）
        service_interval: 服务健康检查间隔（秒）
    """
    
    def __init__(self):
        """
        初始化监控服务
        
        从配置文件和环境变量读取所有配置参数
        """
        # 加载配置文件
        self.config = self._load_config()
        self.line_id = self.config.get("line_id") or os.getenv("LINE_ID", "L1")
        
        # 系统监控配置
        system_cfg = self.config.get("system", {})
        service_cfg = self.config.get("services", {})
        
        self.system_interval = int(system_cfg.get("interval", 30))   # 系统指标采集间隔
        self.service_interval = int(service_cfg.get("interval", 20)) # 服务检查间隔
        self.disk_path = system_cfg.get("disk_path", "/")            # 磁盘监控路径
        self.service_targets: List[Dict[str, Any]] = service_cfg.get("targets", [])  # 要监控的服务列表
        self.http_timeout = float(service_cfg.get("http_timeout", 5))  # HTTP 检查超时
        self.publish_mqtt = bool(system_cfg.get("publish_mqtt", True)) # 是否发布到 MQTT
        self.log_root = Path(os.getenv("LOG_ROOT", "/logs"))

        self.service_failure_threshold = int(service_cfg.get("failure_threshold", 3))
        self.service_recovery_observe = int(service_cfg.get("recovery_observe", 6))
        self.service_max_restart_per_hour = int(service_cfg.get("max_restart_per_hour", 5))
        global_self_heal_cfg = service_cfg.get("self_heal") or {}
        self.self_heal_defaults = {
            "enabled": bool(global_self_heal_cfg.get("enabled", True)),
            "failure_threshold": self.service_failure_threshold,
            "recovery_observe": self.service_recovery_observe,
            "max_restart_per_hour": self.service_max_restart_per_hour,
        }
        self.service_health_state: Dict[str, Any] = {}
        self.self_heal_manager = SelfHealManager()
        self.self_heal_auditor: SelfHealAuditor | None = None
        self.discovery_agent = DiscoveryAgent(line_id=self.line_id)
        self.discovery_agent.set_mqtt_client_supplier(self._get_mqtt_client)
        
        # MQTT 配置
        self.mqtt_broker = os.getenv("MQTT_BROKER", "localhost")
        self.mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
        self.mqtt_user = os.getenv("MQTT_USER", "")
        self.mqtt_password = os.getenv("MQTT_PASSWORD", "")
        self.status_topic = os.getenv(
            "MQTT_TOPIC_STATUS",
            f"system/{self.line_id}/status",  # 状态发布主题
        )
        self.self_heal_topic = os.getenv(
            "MQTT_TOPIC_SELF_HEAL",
            "events/system/self_heal",
        )
        self.self_heal_auditor = SelfHealAuditor(
            line_id=self.line_id,
            event_recorder=event_recorder,
            mqtt_client_getter=self._get_mqtt_client,
            topic=self.self_heal_topic,
        )
        
        # InfluxDB 配置
        self.influx_host = os.getenv("INFLUXDB_HOST", "localhost")
        self.influx_port = os.getenv("INFLUXDB_PORT", "8086")
        self.influx_token = os.getenv("INFLUXDB_TOKEN", "")
        self.influx_org = os.getenv("INFLUXDB_ORG", "edge-org")
        self.influx_bucket = os.getenv("INFLUXDB_BUCKET", "industrial-data")
        self.system_measurement = os.getenv("MONITOR_SYSTEM_MEASUREMENT", "system_metrics")   # 系统指标表
        self.service_measurement = os.getenv("MONITOR_SERVICE_MEASUREMENT", "system_health")  # 服务健康表
        self.status_schema = "edge.monitor.status.v1"  # 状态消息 schema 版本
        # Alertmanager 配置（用于 ops-console 告警聚合）
        self.alertmanager_base = os.getenv("ALERTMANAGER_BASE_URL", "").rstrip("/")
        self.alertmanager_timeout = float(os.getenv("ALERTMANAGER_TIMEOUT", "5") or "5")
        self.alertmanager_user = os.getenv("ALERTMANAGER_USERNAME", "")
        self.alertmanager_pass = os.getenv("ALERTMANAGER_PASSWORD", "")

        # Alarm ingest 通道：状态转移上报到 edge-api，落 alarm_events 表 + outbox 同步到云端
        self.alarm_ingest_url = os.getenv(
            "MONITOR_ALARM_INGEST_URL",
            "http://edge-api:8000/api/v1/alarms/internal/ingest",
        ).strip()
        self.alarm_ingest_token = os.getenv("MONITOR_ALARM_INGEST_TOKEN", "").strip()
        self.alarm_ingest_timeout = float(os.getenv("MONITOR_ALARM_INGEST_TIMEOUT", "5") or "5")
        self.alarm_ingest_header = os.getenv("MONITOR_ALARM_INGEST_HEADER", "X-API-Key").strip() or "X-API-Key"
        
        # 客户端实例
        self.mqtt_client = None
        self.influx_client = None
        self.write_api = None
        self.http_client: httpx.AsyncClient | None = None
        self.docker_client = None

        # P0：连接可靠性（90 天游泳）
        # - 连接失败不能永久停在 None，需要自动重试
        # - 加指数退避 + 下次尝试时间，避免连接风暴
        self._mqtt_reconnect_backoff = 1.0
        self._mqtt_reconnect_backoff_max = float(os.getenv("MONITOR_MQTT_RECONNECT_MAX_SEC", "30") or "30")
        self._next_mqtt_connect_at = 0.0
        self._influx_reconnect_backoff = 1.0
        self._influx_reconnect_backoff_max = float(os.getenv("MONITOR_INFLUX_RECONNECT_MAX_SEC", "30") or "30")
        self._next_influx_connect_at = 0.0
        
        # 运行状态
        self._running = False
        self._tasks: List[asyncio.Task] = []
        
        # 缓存的监控数据
        self.system_metrics: Dict[str, Any] = {}
        self.service_status: Dict[str, Any] = {}

        control_cfg = self.config.get("control", {}) if isinstance(self.config, dict) else {}
        self.control_status_target: Dict[str, Any] = (control_cfg.get("status") or {}) if isinstance(control_cfg, dict) else {}
        self.control_status_enabled = bool(self.control_status_target.get("enabled", False))
        self.control_status: Dict[str, Any] = {
            "enabled": self.control_status_enabled,
            "status": "unknown",
            "self_heal": {"enabled": False, "reason": "control_visibility_probe_only"},
        }
        self.control_visibility_state: Dict[str, Any] = {}
        
        # 预热 CPU 使用率采集（首次调用会阻塞）
        psutil.cpu_percent(interval=None)
    
    async def start(self):
        """
        启动监控服务
        
        启动流程：
        1. 连接 MQTT、InfluxDB、HTTP 客户端、Docker
        2. 启动系统指标采集循环
        3. 启动服务健康检查循环
        """
        logger.info("edge_monitor_starting", line=self.line_id)
        event_recorder.record_event(
            "service_starting",
            tags={"component": "edge-monitor", "line": self.line_id},
        )
        
        # 初始化各种客户端连接
        self._connect_mqtt()
        self._connect_influx()
        await self._connect_http()
        self._connect_docker()
        self.self_heal_manager.set_docker_client_getter(self._get_docker_client)
        await self.discovery_agent.start()
        
        # 启动两个并行的监控循环
        self._running = True
        self._tasks = [
            asyncio.create_task(self._system_metrics_loop()),   # 系统指标采集
            asyncio.create_task(self._service_health_loop()),   # 服务健康检查
        ]
        
        logger.info("edge_monitor_started")
        event_recorder.record_event(
            "service_started",
            tags={"component": "edge-monitor", "line": self.line_id},
        )
    
    async def stop(self):
        """
        停止监控服务
        
        按顺序清理资源：后台任务 -> HTTP客户端 -> InfluxDB -> MQTT -> Docker
        """
        logger.info("edge_monitor_stopping")
        event_recorder.record_event(
            "service_stopping",
            tags={"component": "edge-monitor", "line": self.line_id},
        )
        self._running = False
        
        # 取消所有后台任务
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        
        # 关闭 HTTP 客户端
        if self.http_client:
            await self.http_client.aclose()
            self.http_client = None
        
        # 关闭 InfluxDB 客户端
        if self.write_api and self.influx_client:
            self.influx_client.close()
            self.write_api = None
        
        # 关闭 MQTT 客户端
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            self.mqtt_client = None
        
        # 关闭 Docker 客户端
        if self.docker_client:
            self.docker_client.close()
            self.docker_client = None

        await self.discovery_agent.stop()
        
        logger.info("edge_monitor_stopped")
        event_recorder.record_event(
            "service_stopped",
            tags={"component": "edge-monitor", "line": self.line_id},
        )
    
    def _load_config(self) -> Dict[str, Any]:
        """
        加载监控配置文件
        
        Returns:
            配置字典，如果文件不存在返回空字典
        """
        if DEFAULT_CONFIG_PATH.exists():
            with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        logger.warning("monitor_config_missing", path=str(DEFAULT_CONFIG_PATH))
        return {}

    def read_service_logs(self, name: str, lines: int = 200) -> List[Dict[str, str]]:
        """
        读取指定服务的日志尾部
        """
        log_file = self._resolve_log_file(name)
        if not log_file or not log_file.exists():
            raise FileNotFoundError(f"log file for {name} not found")
        max_lines = max(1, min(lines, 2000))
        buffer: deque[str] = deque(maxlen=max_lines)
        with log_file.open("r", encoding="utf-8", errors="ignore") as handle:
            for raw_line in handle:
                buffer.append(raw_line.rstrip("\n"))
        entries: List[Dict[str, str]] = []
        for raw in buffer:
            if not raw:
                continue
            parts = raw.split(" ", 1)
            if len(parts) == 2:
                ts, msg = parts
            else:
                ts, msg = "", raw
            entries.append({"ts": ts, "msg": msg, "level": "INFO"})
        return entries

    def _resolve_log_file(self, name: str) -> Optional[Path]:
        """
        根据服务名推断日志文件位置
        """
        candidates = [
            self.log_root / name / "app.log",
            self.log_root / f"{name}.log",
        ]
        for path in candidates:
            if path.exists():
                return path
        return None
    
    def _connect_mqtt(self):
        """连接到 MQTT Broker"""
        # 避免在 connect_async 尚未完成时反复 new Client 导致“连接风暴”，并且 publish 队列被不断丢失
        if self.mqtt_client is not None:
            return
        try:
            self.mqtt_client = mqtt.Client(client_id="edge-monitor")
            if self.mqtt_user and self.mqtt_password:
                self.mqtt_client.username_pw_set(self.mqtt_user, self.mqtt_password)
            try:
                if hasattr(self.mqtt_client, "reconnect_delay_set"):
                    self.mqtt_client.reconnect_delay_set(min_delay=1, max_delay=int(self._mqtt_reconnect_backoff_max))
            except (TypeError, ValueError):
                pass

            # 连接回调：仅在真正连上 broker 后才认为“connected”
            def _on_connect(client, _userdata, _flags, rc):  # pragma: no cover
                if rc == 0:
                    logger.info("monitor_mqtt_connected", broker=self.mqtt_broker, port=self.mqtt_port)
                    self._mqtt_reconnect_backoff = 1.0
                    self._next_mqtt_connect_at = 0.0
                    return
                # 非 0：鉴权/参数错误等，主动断开并进入退避重试
                logger.error("monitor_mqtt_connect_rc", rc=rc)
                try:
                    client.disconnect()
                except (OSError, RuntimeError, ValueError):
                    pass
                self.mqtt_client = None
                self._next_mqtt_connect_at = time.time() + self._mqtt_reconnect_backoff
                self._mqtt_reconnect_backoff = min(self._mqtt_reconnect_backoff * 2, self._mqtt_reconnect_backoff_max)

            def _on_disconnect(_client, _userdata, rc):  # pragma: no cover
                logger.warning("monitor_mqtt_disconnected", rc=rc)
                self.mqtt_client = None
                self._next_mqtt_connect_at = time.time() + self._mqtt_reconnect_backoff
                self._mqtt_reconnect_backoff = min(self._mqtt_reconnect_backoff * 2, self._mqtt_reconnect_backoff_max)

            self.mqtt_client.on_connect = _on_connect
            self.mqtt_client.on_disconnect = _on_disconnect

            self.mqtt_client.connect_async(self.mqtt_broker, self.mqtt_port, keepalive=60)
            self.mqtt_client.loop_start()
            # connect_async 是异步的：这里不要假装已连接；并设置最小重试间隔避免频繁重试
            self._next_mqtt_connect_at = time.time() + 1.0
        except (OSError, RuntimeError, ValueError) as exc:
            logger.error("monitor_mqtt_connect_failed", error=str(exc))
            self.mqtt_client = None
            self._next_mqtt_connect_at = time.time() + self._mqtt_reconnect_backoff
            self._mqtt_reconnect_backoff = min(self._mqtt_reconnect_backoff * 2, self._mqtt_reconnect_backoff_max)
    
    def _connect_influx(self):
        """连接到 InfluxDB"""
        try:
            url = f"http://{self.influx_host}:{self.influx_port}"
            self.influx_client = InfluxDBClient(
                url=url,
                token=self.influx_token,
                org=self.influx_org,
            )
            self.write_api = self.influx_client.write_api(write_options=ASYNCHRONOUS)
            logger.info("monitor_influx_connected", url=url, bucket=self.influx_bucket)
            self._influx_reconnect_backoff = 1.0
            self._next_influx_connect_at = 0.0
        except (ApiException, OSError, RuntimeError, ValueError) as exc:
            logger.error("monitor_influx_connect_failed", error=str(exc))
            self.influx_client = None
            self.write_api = None
            self._next_influx_connect_at = time.time() + self._influx_reconnect_backoff
            self._influx_reconnect_backoff = min(self._influx_reconnect_backoff * 2, self._influx_reconnect_backoff_max)
    
    async def _connect_http(self):
        """初始化 HTTP 客户端（用于服务健康检查）"""
        self.http_client = httpx.AsyncClient(timeout=self.http_timeout)
    
    def _connect_docker(self):
        """连接到本地 Docker daemon（用于容器状态检查）"""
        try:
            self.docker_client = docker.from_env()
        except docker_errors.DockerException as exc:
            logger.error("monitor_docker_connect_failed", error=str(exc))
            self.docker_client = None

    def _get_docker_client(self):
        """提供当前 Docker 客户端实例"""
        return self.docker_client

    def _get_mqtt_client(self):
        """提供当前 MQTT 客户端实例"""
        return self.mqtt_client

    async def _ensure_mqtt(self):
        """
        P0：确保 MQTT 连接可用（失败自动重试+退避）。
        """
        if not self.publish_mqtt:
            return False
        now = time.time()
        if self._next_mqtt_connect_at and now < self._next_mqtt_connect_at:
            return False
        if self.mqtt_client and getattr(self.mqtt_client, "is_connected", lambda: False)():
            self._mqtt_reconnect_backoff = 1.0
            self._next_mqtt_connect_at = 0.0
            return True
        self._connect_mqtt()
        # 给网络线程一点时间推进连接（不强等待）
        await asyncio.sleep(0)
        return bool(self.mqtt_client and getattr(self.mqtt_client, "is_connected", lambda: False)())

    async def _ensure_influx(self):
        """
        P0：确保 InfluxDB 写入通路可用（失败自动重试+退避）。
        """
        now = time.time()
        if self._next_influx_connect_at and now < self._next_influx_connect_at:
            return False
        if self.influx_client and self.write_api:
            self._influx_reconnect_backoff = 1.0
            self._next_influx_connect_at = 0.0
            return True
        self._connect_influx()
        await asyncio.sleep(0)
        return bool(self.influx_client and self.write_api)
    
    async def _fetch_alertmanager_alerts(self, severity: str | None, service: str | None, state: str | None) -> list:
        """
        从 Alertmanager 拉取告警，支持按 severity/service/state 过滤。
        """
        if not self.alertmanager_base:
            raise HTTPException(status_code=503, detail="alertmanager not configured")
        if not self.http_client:
            await self._connect_http()
        params = []
        if severity:
            params.append(("filter", f"severity={severity}"))
        if service:
            params.append(("filter", f"service={service}"))
        # Alertmanager v2 支持 active/silenced/inhibited 查询参数
        state = (state or "active").lower()
        params.append(("active", "true" if state == "active" else "false"))
        params.append(("silenced", "true" if state == "silenced" else "false"))
        params.append(("inhibited", "true" if state == "inhibited" else "false"))
        auth = None
        if self.alertmanager_user and self.alertmanager_pass:
            auth = (self.alertmanager_user, self.alertmanager_pass)
        url = f"{self.alertmanager_base}/api/v2/alerts"
        try:
            resp = await self.http_client.get(url, params=params, auth=auth, timeout=self.alertmanager_timeout)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("alertmanager_http_error", status=exc.response.status_code, detail=str(exc))
            raise HTTPException(status_code=exc.response.status_code, detail="alertmanager error")
        except (httpx.HTTPError, ValueError) as exc:
            logger.error("alertmanager_fetch_failed", error=str(exc))
            raise HTTPException(status_code=502, detail="failed to fetch alerts")
    
    async def _system_metrics_loop(self):
        """
        系统指标采集主循环
        
        按配置的间隔周期性采集 CPU、内存、磁盘等系统指标，
        并将数据写入 InfluxDB 和/或发布到 MQTT。
        """
        while self._running:
            # 采集系统指标
            metrics = self._collect_system_metrics()
            self.system_metrics = metrics
            
            # 写入 InfluxDB
            await self._ensure_influx()
            self._write_system_metrics(metrics)
            
            # 发布到 MQTT（如果启用）
            if self.publish_mqtt:
                await self._ensure_mqtt()
                self._publish_status()
            
            await asyncio.sleep(self.system_interval)
    
    async def _service_health_loop(self):
        """
        服务健康检查主循环
        
        并行检查所有配置的服务目标（HTTP/TCP/容器），
        汇总状态后写入数据库并发布。
        """
        while self._running:
            # 并行执行所有服务的健康检查
            tasks = [self._check_service(target) for target in self.service_targets]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 汇总检查结果
            now = int(time.time())
            aggregated = {}
            for target, result in zip(self.service_targets, results):
                name = target.get("name", "unknown")
                if isinstance(result, dict):
                    # 正常返回的检查结果
                    aggregated[name] = result
                else:
                    # 检查过程中发生异常
                    aggregated[name] = {
                        "name": name,
                        "status": "down",
                        "timestamp": now,
                        "checks": {
                            "monitor": {
                                "status": "down",
                                "detail": str(result),
                            }
                        },
                    }
            
            # 更新自愈状态缓存 + 抓 healthy↔degraded/down 转移
            state_snapshot, alarm_transitions = self._update_service_states(aggregated)
            for name, state in state_snapshot.items():
                aggregated[name]["self_heal"] = {"state": state}

            if self.control_status_enabled:
                control_record = await self._check_control_status()
                control_transition = self._update_control_visibility_state(control_record)
                if control_transition is not None:
                    alarm_transitions.append(control_transition)

            await self._emit_alarm_transitions(alarm_transitions)
            await self._process_self_heal(aggregated)
            
            # 更新缓存并持久化
            self.service_status = aggregated
            await self._ensure_influx()
            self._write_service_health(aggregated)
            
            # 发布到 MQTT
            if self.publish_mqtt:
                await self._ensure_mqtt()
                self._publish_status()
            
            await asyncio.sleep(self.service_interval)
    
    def _collect_system_metrics(self) -> Dict[str, Any]:
        """
        采集系统指标
        
        使用 psutil 库获取以下指标：
        - CPU 使用率和负载均衡
        - 内存使用情况
        - 磁盘使用情况
        - 系统运行时间
        
        Returns:
            包含所有指标的字典
        """
        # CPU 指标
        cpu_percent = psutil.cpu_percent(interval=0.1)
        # 系统负载（1/5/15 分钟平均）
        load_avg = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
        
        # 内存指标
        memory = psutil.virtual_memory()
        
        # 磁盘指标
        disk = psutil.disk_usage(self.disk_path)
        
        # 系统运行时间
        captured_at = time.time()
        uptime = int(captured_at - psutil.boot_time())
        
        metrics = {
            "cpu": {
                "percent": cpu_percent,
                "load_avg": {
                    "1m": load_avg[0],
                    "5m": load_avg[1],
                    "15m": load_avg[2],
                },
            },
            "memory": {
                "percent": memory.percent,
                "used": memory.used,
                "total": memory.total,
            },
            "disk": {
                "percent": disk.percent,
                "used": disk.used,
                "total": disk.total,
                "path": self.disk_path,
            },
            "uptime_sec": uptime,
            "updated_at": int(captured_at),
        }
        logger.debug("system_metrics_collected", **metrics)
        return metrics
    
    async def _check_service(self, target: Dict[str, Any]) -> Dict[str, Any]:
        """
        检查单个服务的健康状态
        
        支持三种检查方式（可组合）：
        - HTTP 健康检查：访问指定 URL 检查返回状态码
        - TCP 端口检查：尝试建立 TCP 连接
        - 容器状态检查：查询 Docker 容器运行状态
        
        综合所有检查结果，取最严重的状态作为最终状态。
        
        Args:
            target: 服务配置，包含 name、http、tcp、container 等字段
            
        Returns:
            服务状态字典，包含 name、status、checks 等
        """
        name = target.get("name", "unknown")
        timestamp = int(time.time())
        overall_status = "unknown"
        checks: Dict[str, Any] = {}
        
        # HTTP 健康检查
        http_cfg = target.get("http", {})
        http_url = http_cfg.get("url") or target.get("url")
        if http_url:
            http_result = await self._http_health_check(http_url)
            checks["http"] = http_result
            overall_status = self._merge_status(overall_status, http_result.get("status"))
        
        # TCP 端口检查
        tcp_cfg = target.get("tcp")
        if tcp_cfg:
            tcp_result = await self._tcp_health_check(
                host=tcp_cfg.get("host") or name,
                port=tcp_cfg.get("port"),
            )
            checks["tcp"] = tcp_result
            overall_status = self._merge_status(overall_status, tcp_result.get("status"))
        
        # 容器状态检查
        container_name = target.get("container")
        if container_name:
            container_result = self._container_health_check(container_name)
            checks["container"] = container_result
            overall_status = self._merge_status(overall_status, container_result.get("status"))
        
        # 如果没有配置任何检查方式，标记为降级
        if not checks:
            checks["monitor"] = {
                "status": "degraded",
                "detail": "no_checks_configured",
            }
        
        # 未知状态默认为降级
        if overall_status == "unknown":
            overall_status = "degraded"
        
        logger.info("service_check", service=name, status=overall_status)
        return {
            "name": name,
            "status": overall_status,
            "timestamp": timestamp,
            "checks": checks,
            "latency_ms": checks.get("http", {}).get("latency_ms"),
            "container_state": checks.get("container", {}).get("state", ""),
        }

    async def _check_control_status(self) -> Dict[str, Any]:
        """
        拉取 edge-api 的只读控制状态快照。

        这是控制可见性探测，不是服务健康探测：结果进入 status payload 和 alarm ingest，
        但不进入 service_targets，也不参与 self-heal。
        """
        name = str(self.control_status_target.get("name") or "control-status")
        url = str(self.control_status_target.get("url") or "").strip()
        timestamp = int(time.time())
        base: Dict[str, Any] = {
            "name": name,
            "enabled": self.control_status_enabled,
            "status": "unknown",
            "timestamp": timestamp,
            "self_heal": {"enabled": False, "reason": "control_visibility_probe_only"},
        }

        if not self.control_status_enabled:
            base["status"] = "healthy"
            base["detail"] = "control_status_probe_disabled"
            return base
        if not url:
            base["status"] = "degraded"
            base["detail"] = "control_status_url_missing"
            return base
        if not self.http_client:
            base["status"] = "degraded"
            base["detail"] = "http_client_not_ready"
            return base

        timeout = float(self.control_status_target.get("timeout", self.http_timeout) or self.http_timeout)
        headers = self._control_status_headers()
        try:
            start = time.perf_counter()
            response = await self.http_client.get(url, headers=headers, timeout=timeout)
            latency_ms = (time.perf_counter() - start) * 1000
        except httpx.HTTPError as exc:
            base["status"] = "down"
            base["detail"] = "control_status_unreachable"
            base["error"] = str(exc)
            base["checks"] = {
                "http": {
                    "status": "down",
                    "url": url,
                    "detail": str(exc),
                }
            }
            return base

        base["latency_ms"] = round(latency_ms, 2)
        base["checks"] = {
            "http": {
                "status": "healthy" if 200 <= response.status_code < 300 else "degraded",
                "url": url,
                "status_code": response.status_code,
                "latency_ms": round(latency_ms, 2),
            }
        }
        if response.status_code < 200 or response.status_code >= 300:
            base["status"] = "degraded"
            base["detail"] = f"control_status_http_{response.status_code}"
            return base

        try:
            body = response.json()
        except ValueError as exc:
            base["status"] = "degraded"
            base["detail"] = "control_status_invalid_json"
            base["error"] = str(exc)
            return base

        if not isinstance(body, dict) or body.get("success") is not True or not isinstance(body.get("data"), dict):
            base["status"] = "degraded"
            base["detail"] = "control_status_payload_invalid"
            return base

        data = body["data"]
        analysis = self._analyze_control_status_payload(data)
        base.update(analysis)
        base["raw"] = data
        return base

    def _control_status_headers(self) -> Dict[str, str]:
        key = self._resolve_control_status_api_key()
        header = str(
            self.control_status_target.get("api_key_header")
            or os.getenv("MONITOR_API_KEY_HEADER", "X-API-Key")
            or "X-API-Key"
        ).strip()
        if not key or not header:
            return {}
        return {header: key}

    def _resolve_control_status_api_key(self) -> Optional[str]:
        explicit = str(self.control_status_target.get("api_key") or "").strip()
        if explicit:
            return explicit

        env_cfg = self.control_status_target.get("api_key_env") or "ADMIN_API_KEYS"
        if isinstance(env_cfg, str):
            env_names = [item.strip() for item in env_cfg.split(",") if item.strip()]
        elif isinstance(env_cfg, list):
            env_names = [str(item).strip() for item in env_cfg if str(item).strip()]
        else:
            env_names = ["ADMIN_API_KEYS"]

        for env_name in env_names:
            keys = _split_api_keys(os.getenv(env_name))
            if keys:
                return keys[0]
        return None

    def _analyze_control_status_payload(self, data: Dict[str, Any]) -> Dict[str, Any]:
        mode = str(data.get("mode") or "unknown")
        authority = data.get("authority_snapshot") if isinstance(data.get("authority_snapshot"), dict) else {}
        transport = data.get("transport") if isinstance(data.get("transport"), dict) else {}
        owner = authority.get("AUTHORITY_OWNER") or authority.get("owner")
        last_command = self._last_command_result_summary(data.get("last_command_result"))

        reasons: list[str] = []
        if mode == "AUTO_PAUSED":
            reasons.append(str(data.get("pause_reason") or "AUTO_PAUSED"))
        if owner and owner != "EDGE":
            reasons.append(f"AUTHORITY_OWNER_NOT_EDGE:{owner}")
        if transport:
            if transport.get("mqtt_connected") is False:
                reasons.append("MQTT_NOT_CONNECTED")
            if transport.get("signature_configured") is False:
                reasons.append("CONTROL_SIGNATURE_SECRET_MISSING")
            if transport.get("ready") is False and not any(
                reason in {"MQTT_NOT_CONNECTED", "CONTROL_SIGNATURE_SECRET_MISSING"}
                for reason in reasons
            ):
                reasons.append("CONTROL_TRANSPORT_NOT_READY")
        if last_command.get("problem"):
            reasons.append(str(last_command.get("reason") or "CONTROL_COMMAND_ERROR"))

        status = "degraded" if reasons else "healthy"
        return {
            "status": status,
            "mode": mode,
            "auto_dispatch_status": data.get("auto_dispatch_status"),
            "pause_reason": data.get("pause_reason"),
            "requires_reauthorization": data.get("requires_reauthorization"),
            "authority_owner": owner,
            "authority_snapshot": authority or None,
            "transport": transport or None,
            "last_command_result": last_command,
            "last_auto_action": data.get("last_auto_action"),
            "manual_command_entrypoint": data.get("manual_command_entrypoint"),
            "auto_dispatch_entrypoint": data.get("auto_dispatch_entrypoint"),
            "reasons": reasons,
            "detail": "; ".join(reasons) if reasons else "control_status_ok",
        }

    def _last_command_result_summary(self, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {"present": False, "problem": False}

        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        values = [
            str(item).lower()
            for item in self._scalar_values(result)
            if item is not None and not isinstance(item, bool)
        ]
        value_text = " ".join(values)
        error_value = result.get("error") if isinstance(result, dict) else None
        status_value = str(result.get("status") or "").lower() if isinstance(result, dict) else ""
        success_value = result.get("success") if isinstance(result, dict) else None

        reason: Optional[str] = None
        if "timeout" in value_text or "timed_out" in value_text:
            reason = "CONTROL_COMMAND_TIMEOUT"
        elif "nak" in value_text or "nack" in value_text or "rejected" in value_text:
            reason = "CONTROL_COMMAND_NAK"
        elif error_value:
            reason = "CONTROL_COMMAND_ERROR"
        elif status_value in {"error", "failed", "failure"}:
            reason = "CONTROL_COMMAND_ERROR"
        elif success_value is False:
            reason = "CONTROL_COMMAND_FAILED"

        return {
            "present": True,
            "problem": reason is not None,
            "reason": reason,
            "scope": payload.get("scope"),
            "recorded_at": payload.get("recorded_at"),
            "status": result.get("status") if isinstance(result, dict) else None,
            "success": result.get("success") if isinstance(result, dict) else None,
            "error": error_value,
        }

    def _scalar_values(self, value: Any) -> list[Any]:
        if isinstance(value, dict):
            values: list[Any] = []
            for item in value.values():
                values.extend(self._scalar_values(item))
            return values
        if isinstance(value, list):
            values = []
            for item in value:
                values.extend(self._scalar_values(item))
            return values
        return [value]
    
    async def _http_health_check(self, url: str) -> Dict[str, Any]:
        """
        执行 HTTP 健康检查
        
        访问指定 URL 并根据响应状态码判断服务健康状态：
        - 2xx: healthy（健康）
        - 其他: degraded（降级）
        - 异常: down（宕机）
        
        Args:
            url: 健康检查 URL（通常是 /health 端点）
            
        Returns:
            检查结果字典，包含 status、latency_ms、status_code 等
        """
        result: Dict[str, Any] = {"status": "unknown", "url": url}
        
        # 检查 HTTP 客户端是否就绪
        if not self.http_client:
            result["status"] = "degraded"
            result["detail"] = "http_client_not_ready"
            return result
        
        try:
            # 记录请求延迟
            start = time.perf_counter()
            response = await self.http_client.get(url)
            latency_ms = (time.perf_counter() - start) * 1000
            
            result["latency_ms"] = round(latency_ms, 2)
            result["status_code"] = response.status_code
            # 关键：在非 2xx 时保留部分响应体，供云端/现场快速定位（如 PLC_OFFLINE）
            # 注意：只截断少量文本，避免状态 payload 过大
            if response.status_code < 200 or response.status_code >= 300:
                try:
                    text = (response.text or "")[:2048]
                    if text:
                        result["response_body"] = text
                    ct = (response.headers.get("content-type") or "").lower()
                    if "application/json" in ct and text:
                        try:
                            result["response_json"] = response.json()
                        except ValueError as exc:
                            result["response_json_error"] = str(exc)
                            logger.warning("http_health_response_json_parse_failed", url=url, error=str(exc))
                except (RuntimeError, UnicodeDecodeError) as exc:
                    result["response_body_error"] = str(exc)
                    logger.warning("http_health_response_body_read_failed", url=url, error=str(exc))
            
            # 根据状态码判断健康状态
            if 200 <= response.status_code < 300:
                result["status"] = "healthy"
            else:
                result["status"] = "degraded"
                # 优先显示标准化 reason（如果有），否则退化为 http xxx
                reason = None
                try:
                    rj = result.get("response_json") or {}
                    if isinstance(rj, dict):
                        reason = rj.get("reason")
                except (AttributeError, TypeError):
                    reason = None
                result["detail"] = f"{reason}" if reason else f"http {response.status_code}"
        except httpx.HTTPError as exc:
            # 请求失败（连接超时、拒绝等）
            result["status"] = "down"
            result["detail"] = str(exc)
        
        return result
    
    async def _tcp_health_check(self, host: Optional[str], port: Optional[int]) -> Dict[str, Any]:
        """
        执行 TCP 端口健康检查
        
        尝试建立 TCP 连接来验证服务是否可达。
        适用于没有 HTTP 接口的服务（如数据库、消息队列）。
        
        Args:
            host: 目标主机地址
            port: 目标端口
            
        Returns:
            检查结果字典，包含 status、latency_ms 等
        """
        result: Dict[str, Any] = {"status": "unknown", "host": host, "port": port}
        
        # 参数校验
        if not host or port is None:
            result["status"] = "degraded"
            result["detail"] = "tcp_host_or_port_missing"
            return result
        
        try:
            port_int = int(port)
            start = time.perf_counter()
            
            # 尝试建立 TCP 连接（带超时）
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port_int),
                timeout=self.http_timeout,
            )
            
            # 立即关闭连接
            writer.close()
            if hasattr(writer, "wait_closed"):
                await writer.wait_closed()
            
            # 计算连接延迟
            latency_ms = (time.perf_counter() - start) * 1000
            result["latency_ms"] = round(latency_ms, 2)
            result["status"] = "healthy"
            
        except (OSError, asyncio.TimeoutError) as exc:
            # 连接失败（超时、拒绝、主机不可达等）
            result["status"] = "down"
            result["detail"] = str(exc)
        
        return result
    
    def _container_health_check(self, container_name: str) -> Dict[str, Any]:
        """
        检查 Docker 容器运行状态
        
        通过 Docker API 查询容器状态，判断服务是否正常运行。
        
        Args:
            container_name: Docker 容器名称
            
        Returns:
            检查结果字典，包含 status、state 等
        """
        result: Dict[str, Any] = {"status": "unknown", "name": container_name}
        
        # 检查 Docker 客户端是否就绪
        if not self.docker_client:
            # 没有 docker_client 时，容器状态无法判定，但健康检查应提示“降级”
            # 以便上层 merge_status 能把它当作风险信号（而不是完全未知）
            result["status"] = "degraded"
            result["detail"] = "docker_client_not_ready"
            return result
        
        try:
            # 获取容器信息
            container = self.docker_client.containers.get(container_name)
            result["state"] = container.status
            # running 状态为健康，其他（paused、exited 等）为降级
            result["status"] = "healthy" if container.status == "running" else "degraded"
            
        except docker_errors.NotFound:
            # 容器不存在
            result["state"] = "missing"
            result["status"] = "down"
            result["detail"] = "container_not_found"
            
        except docker_errors.DockerException as exc:
            # Docker API 错误
            result["status"] = "unknown"
            result["detail"] = f"docker:{exc}"
        
        return result
    
    def _merge_status(self, current: str, candidate: Optional[str]) -> str:
        """
        合并状态，取最严重的状态
        
        根据状态优先级，返回更严重的状态。
        优先级：unknown < healthy < degraded < down
        
        Args:
            current: 当前状态
            candidate: 候选状态
            
        Returns:
            合并后的状态（取更严重的）
        """
        if not candidate:
            return current
        # 比较优先级，取更高的（更严重的）
        if STATUS_PRIORITY.get(candidate, 0) > STATUS_PRIORITY.get(current, 0):
            return candidate
        return current

    def _resolve_self_heal_config(self, target: Dict[str, Any]) -> Dict[str, Any]:
        """合并目标的自愈配置与全局默认值"""
        target_cfg = target.get("self_heal") or {}
        return {
            "enabled": bool(target_cfg.get("enabled", self.self_heal_defaults["enabled"])),
            "failure_threshold": int(
                target_cfg.get("failure_threshold", self.self_heal_defaults["failure_threshold"])
            ),
            "recovery_observe": int(
                target_cfg.get("recovery_observe", self.self_heal_defaults["recovery_observe"])
            ),
            "max_restart_per_hour": int(
                target_cfg.get("max_restart_per_hour", self.self_heal_defaults["max_restart_per_hour"])
            ),
        }

    def _update_service_states(self, aggregated: Dict[str, Any]) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        根据最新检测结果维护各服务的故障计数与自愈上下文。

        返回 (snapshot, alarm_transitions)。alarm_transitions 是本轮 healthy ↔ degraded/down
        的状态转移列表，由调用方异步 emit 到 edge-api alarm ingest 端点；本方法不动
        self_heal / failure count / cooldown 任何逻辑。
        """
        snapshot: Dict[str, Any] = {}
        transitions: List[Dict[str, Any]] = []
        now = int(time.time())

        for target in self.service_targets:
            name = target.get("name", "unknown")
            record = aggregated.get(name)
            if not record:
                continue

            status = (record.get("status") or "unknown").lower()
            resolved_cfg = self._resolve_self_heal_config(target)
            state = self.service_health_state.get(
                name,
                {
                    "consecutive_failures": 0,
                    "last_success": None,
                    "last_failure": None,
                    "restart_window_start": now,
                    "restart_count_hour": 0,
                    "cooldown_until": None,
                    "last_action": None,
                    "last_action_reason": None,
                    "last_action_details": None,
                    "last_action_at": None,
                },
            )

            # 在 mutation 之前抓 previous_status，纯只读用于 alarm transition 检测；
            # 不参与 self_heal / failure count 决策（这些逻辑保持原样）。
            previous_status = state.get("status")
            transition = self._build_alarm_transition(name, previous_status, status, record)
            if transition is not None:
                transitions.append(transition)

            window_start = state.get("restart_window_start") or now
            if now - window_start >= 3600:
                state["restart_window_start"] = now
                state["restart_count_hour"] = 0

            fail_count = int(state.get("consecutive_failures", 0))
            if status in ("healthy", "ok"):
                fail_count = 0
                state["last_success"] = now
            elif status in ("down", "fail", "failed", "degraded"):
                fail_count += 1
                state["last_failure"] = now

            state["consecutive_failures"] = fail_count
            state["status"] = status
            state["updated_at"] = now
            state["thresholds"] = resolved_cfg
            state["self_heal_enabled"] = resolved_cfg["enabled"]
            state["recovery_observe"] = resolved_cfg["recovery_observe"]
            state["recovery_duration_sec"] = resolved_cfg["recovery_observe"] * self.service_interval
            state["service_interval_sec"] = self.service_interval

            snapshot[name] = {
                "enabled": resolved_cfg["enabled"],
                "status": status,
                "consecutive_failures": fail_count,
                "failure_threshold": resolved_cfg["failure_threshold"],
                "recovery_observe": resolved_cfg["recovery_observe"],
                "max_restart_per_hour": resolved_cfg["max_restart_per_hour"],
                "last_success": state.get("last_success"),
                "last_failure": state.get("last_failure"),
                "restart_count_hour": state.get("restart_count_hour", 0),
                "restart_window_start": state.get("restart_window_start"),
                "cooldown_until": state.get("cooldown_until"),
                "last_action": state.get("last_action"),
                "last_action_reason": state.get("last_action_reason"),
                "last_action_at": state.get("last_action_at"),
                "updated_at": now,
            }

            self.service_health_state[name] = state

        return snapshot, transitions

    def _build_alarm_transition(
        self,
        name: str,
        previous_raw: str | None,
        current_raw: str,
        record: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        比较 mutation 前的 previous_status 和当前 status，生成 firing/resolved transition。

        first-run (previous is None) 视同 healthy，仅当当前 degraded/down 时发 firing。
        相同状态返回 None（不发 alarm）。
        """
        prev = _normalize_health_status(previous_raw)
        curr = _normalize_health_status(current_raw)
        if prev == curr:
            return None

        alarm_key = f"{self.line_id}:{name}:health"
        # message 优先取 http check 的 detail，否则容器/tcp/monitor 的 detail
        message: Optional[str] = None
        checks = record.get("checks") if isinstance(record, dict) else {}
        if isinstance(checks, dict):
            for slot in ("http", "container", "tcp", "monitor"):
                slot_cfg = checks.get(slot)
                if isinstance(slot_cfg, dict) and slot_cfg.get("detail"):
                    message = str(slot_cfg["detail"])[:512]
                    break

        if curr in ("degraded", "down"):
            severity = _STATUS_TO_SEVERITY.get(curr, "warning")
            return {
                "service": name,
                "alarm_key": alarm_key,
                "state": "firing",
                "severity": severity,
                "message": message or f"{name} health changed from {prev} to {curr}",
                "previous_status": prev,
                "current_status": curr,
            }
        # curr == "healthy" 且 prev in (degraded, down) → resolved
        return {
            "service": name,
            "alarm_key": alarm_key,
            "state": "resolved",
            "severity": None,
            "message": message,
            "previous_status": prev,
            "current_status": curr,
        }

    def _update_control_visibility_state(self, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        previous_status = self.control_visibility_state.get("status")
        status = str(record.get("status") or "unknown").lower()
        transition = self._build_control_visibility_transition(previous_status, status, record)

        now = int(time.time())
        self.control_visibility_state.update(
            {
                "status": status,
                "updated_at": now,
                "last_success": now if status == "healthy" else self.control_visibility_state.get("last_success"),
                "last_failure": now if status in ("degraded", "down") else self.control_visibility_state.get("last_failure"),
                "self_heal_enabled": False,
            }
        )
        record["self_heal"] = {
            "enabled": False,
            "state": dict(self.control_visibility_state),
            "reason": "control_visibility_probe_only",
        }
        self.control_status = record
        return transition

    def _build_control_visibility_transition(
        self,
        previous_raw: str | None,
        current_raw: str,
        record: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        prev = _normalize_health_status(previous_raw)
        curr = _normalize_health_status(current_raw)
        if prev == curr:
            return None

        alarm_key = f"{self.line_id}:control:visibility"
        mode = record.get("mode") or "unknown"
        reasons = record.get("reasons") if isinstance(record.get("reasons"), list) else []
        detail = record.get("detail")
        message = str(detail or f"control visibility changed from {prev} to {curr}")[:512]
        meta = {
            "previous_status": prev,
            "current_status": curr,
            "mode": mode,
            "auto_dispatch_status": record.get("auto_dispatch_status"),
            "pause_reason": record.get("pause_reason"),
            "requires_reauthorization": record.get("requires_reauthorization"),
            "authority_owner": record.get("authority_owner"),
            "transport": record.get("transport"),
            "last_command_result": record.get("last_command_result"),
            "reasons": reasons,
            "self_heal_enabled": False,
        }
        if curr in ("degraded", "down"):
            return {
                "service": "control-status",
                "alarm_key": alarm_key,
                "state": "firing",
                "severity": _STATUS_TO_SEVERITY.get(curr, "warning"),
                "message": message,
                "previous_status": prev,
                "current_status": curr,
                "meta": meta,
            }
        return {
            "service": "control-status",
            "alarm_key": alarm_key,
            "state": "resolved",
            "severity": None,
            "message": message if message != "control_status_ok" else None,
            "previous_status": prev,
            "current_status": curr,
            "meta": meta,
        }

    async def _emit_alarm_transitions(self, transitions: List[Dict[str, Any]]) -> None:
        """
        把本轮 transitions POST 到 edge-api /api/v1/alarms/internal/ingest。
        缺配置时静默跳过；单条失败不影响其它条；不阻塞 self_heal 主循环。
        """
        if not transitions:
            return
        if not self.alarm_ingest_url or not self.alarm_ingest_token:
            logger.debug("alarm_ingest_skipped_missing_config", count=len(transitions))
            return
        if not self.http_client:
            await self._connect_http()
        if not self.http_client:
            logger.warning("alarm_ingest_skipped_no_http_client", count=len(transitions))
            return

        headers = {self.alarm_ingest_header: self.alarm_ingest_token}
        for t in transitions:
            meta = {
                "service": t["service"],
                "previous_status": t["previous_status"],
                "current_status": t["current_status"],
            }
            extra_meta = t.get("meta")
            if isinstance(extra_meta, dict):
                meta.update(extra_meta)
            body = {
                "alarm_key": t["alarm_key"],
                "source_service": "edge-monitor",
                "state": t["state"],
                "line_id": self.line_id,
                "meta": meta,
            }
            if t.get("severity"):
                body["severity"] = t["severity"]
            if t.get("message"):
                body["message"] = t["message"]
            try:
                resp = await self.http_client.post(
                    self.alarm_ingest_url,
                    json=body,
                    headers=headers,
                    timeout=self.alarm_ingest_timeout,
                )
                if resp.status_code >= 300:
                    logger.warning(
                        "alarm_ingest_non_2xx",
                        status=resp.status_code,
                        alarm_key=t["alarm_key"],
                        state=t["state"],
                    )
            except (httpx.HTTPError, RuntimeError) as exc:
                logger.warning(
                    "alarm_ingest_failed",
                    alarm_key=t["alarm_key"],
                    state=t["state"],
                    error=str(exc),
                )

    def _find_service_target(self, name: str) -> Optional[Dict[str, Any]]:
        for target in self.service_targets:
            if target.get("name") == name:
                return target
        return None

    def _ensure_state_entry(self, name: str) -> Dict[str, Any]:
        state = self.service_health_state.get(name)
        if state:
            return state
        default_cfg = self._resolve_self_heal_config({"self_heal": {}})
        state = {
            "consecutive_failures": 0,
            "last_success": None,
            "last_failure": None,
            "restart_window_start": int(time.time()),
            "restart_count_hour": 0,
            "cooldown_until": None,
            "self_heal_enabled": default_cfg["enabled"],
            "failure_threshold": default_cfg["failure_threshold"],
            "recovery_observe": default_cfg["recovery_observe"],
            "max_restart_per_hour": default_cfg["max_restart_per_hour"],
        }
        self.service_health_state[name] = state
        return state

    async def _process_self_heal(self, aggregated: Dict[str, Any]):
        """
        根据最新状态执行自愈策略
        """
        for target in self.service_targets:
            name = target.get("name", "unknown")
            state = self.service_health_state.get(name)
            if not state:
                continue

            try:
                outcome = await self.self_heal_manager.evaluate_and_execute(
                    name=name,
                    target=target,
                    state=state,
                    observe_interval_sec=self.service_interval,
                )
            except Exception as exc:  # noqa: BLE001  # pragma: no cover - 故意宽捕：保护主循环韧性边界，未知异常也须记录并继续
                logger.error("self_heal_evaluation_failed", service=name, error=str(exc))
                continue

            if outcome.state_updates:
                state.update(outcome.state_updates)
                self.service_health_state[name] = state
                current_state = aggregated.get(name, {}).get("self_heal", {}).get("state")
                if isinstance(current_state, dict):
                    current_state.update(outcome.state_updates)

            aggregated.setdefault(name, {}).setdefault("self_heal", {})
            aggregated[name]["self_heal"]["last_outcome"] = outcome.to_dict()

            if self.self_heal_auditor:
                self.self_heal_auditor.emit(name, outcome)

    async def manual_restart(self, service_name: str) -> dict:
        target = self._find_service_target(service_name)
        if not target:
            raise HTTPException(status_code=404, detail=f"service {service_name} not found")
        container_name = target.get("container")
        if not container_name:
            raise HTTPException(status_code=400, detail="service does not define container name")

        result = await self.self_heal_manager.executor.restart_container(container_name)
        outcome = SelfHealOutcome(
            timestamp=int(time.time()),
            decision=SelfHealDecision(
                action="manual_restart",
                reason="manual_command",
                details={"initiator": "ops-console"},
            ),
            executed=True,
            success=result.success,
            execution=result,
            state_updates={"manual": True},
        )
        if self.self_heal_auditor:
            self.self_heal_auditor.emit(service_name, outcome)

        if not result.success:
            raise HTTPException(status_code=500, detail=result.error or "restart failed")
        return {"success": True, "service": service_name}

    def set_self_heal_enabled(self, service_name: str, enabled: bool) -> dict:
        target = self._find_service_target(service_name)
        if not target:
            raise HTTPException(status_code=404, detail=f"service {service_name} not found")
        state = self._ensure_state_entry(service_name)
        state["self_heal_enabled"] = enabled
        self.service_health_state[service_name] = state
        return {"success": True, "service": service_name, "enabled": enabled}
    
    def _write_system_metrics(self, metrics: Dict[str, Any]):
        """
        将系统指标写入 InfluxDB
        
        写入的字段包括：
        - cpu_percent: CPU 使用率
        - load_1m/5m/15m: 系统负载
        - memory_percent: 内存使用率
        - disk_percent: 磁盘使用率
        - uptime_sec: 系统运行时间
        
        Args:
            metrics: 采集到的系统指标字典
        """
        if not self.write_api:
            return
        
        try:
            # 构建 InfluxDB 数据点
            point = (
                Point(self.system_measurement)
                .tag("line", self.line_id)
                .field("cpu_percent", metrics["cpu"]["percent"])
                .field("load_1m", metrics["cpu"]["load_avg"]["1m"])
                .field("load_5m", metrics["cpu"]["load_avg"]["5m"])
                .field("load_15m", metrics["cpu"]["load_avg"]["15m"])
                .field("memory_percent", metrics["memory"]["percent"])
                .field("disk_percent", metrics["disk"]["percent"])
                .field("uptime_sec", metrics["uptime_sec"])
                .time(int(time.time() * 1e9))  # 纳秒时间戳
            )
            self.write_api.write(self.influx_bucket, self.influx_org, point)
            
        except (ApiException, OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.error("system_metrics_write_failed", error=str(exc))
            event_recorder.record_event(
                "system_metrics_write_failed",
                severity="error",
                fields={"error": str(exc)},
                tags={"component": "edge-monitor"},
            )
    
    def _write_service_health(self, statuses: Dict[str, Any]):
        """
        将服务健康状态写入 InfluxDB
        
        为每个服务写入一个数据点，包含：
        - status: 健康状态
        - latency_ms: 响应延迟
        - container_state: 容器状态
        
        Args:
            statuses: 各服务的健康状态字典
        """
        if not self.write_api:
            return
        
        points = []
        now = int(time.time() * 1e9)  # 纳秒时间戳
        
        try:
            # 为每个服务构建数据点
            for name, status in statuses.items():
                state = status.get("status", "unknown")
                # 获取延迟（优先从顶层获取，其次从 http 检查结果获取）
                latency = status.get("latency_ms") or status.get("checks", {}).get("http", {}).get("latency_ms")
                # 获取容器状态
                container_state = status.get("container_state") or status.get("checks", {}).get("container", {}).get("state", "")
                
                point = (
                    Point(self.service_measurement)
                    .tag("line", self.line_id)
                    .tag("service", name)
                    .field("status", state)
                    .field("latency_ms", latency or 0.0)
                    .field("container_state", container_state or "")
                    .time(now)
                )
                points.append(point)
            
            # 批量写入
            if points:
                self.write_api.write(self.influx_bucket, self.influx_org, points)
                
        except (ApiException, OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.error("service_health_write_failed", error=str(exc))
            event_recorder.record_event(
                "service_health_write_failed",
                severity="error",
                fields={"error": str(exc)},
                tags={"component": "edge-monitor"},
            )
    
    def _publish_status(self):
        """
        发布状态到 MQTT
        
        将系统指标和服务健康状态打包成 JSON 消息发布到 MQTT 主题，
        供前端大屏和其他服务订阅。
        """
        if not self.mqtt_client:
            return
        
        # 构建状态消息
        payload = self._build_status_payload()
        
        try:
            # 发布到 MQTT（QoS=1 确保送达）
            self.mqtt_client.publish(self.status_topic, json.dumps(payload), qos=1)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.error("status_publish_failed", error=str(exc))
            event_recorder.record_event(
                "status_publish_failed",
                severity="error",
                fields={"error": str(exc)},
                tags={"component": "edge-monitor"},
            )

    def _build_status_payload(self) -> Dict[str, Any]:
        """
        构建状态消息载荷
        
        将系统指标和服务状态组装成标准化的 JSON 结构。
        
        消息结构：
        {
            "schema": "edge.monitor.status.v1",
            "line": "L1",
            "timestamp": 1234567890,
            "services": [...],
            "system": {...}
        }
        
        Returns:
            状态消息字典
        """
        # 构建服务状态列表（按名称排序）
        services = []
        for name in sorted(self.service_status.keys()):
            record = self.service_status[name]
            services.append(
                {
                    "name": name,
                    "status": record.get("status", "unknown"),
                    "checks": record.get("checks", {}),
                    "latency_ms": record.get("latency_ms"),
                    "container_state": record.get("container_state"),
                    "updated_at": record.get("timestamp"),
                    "self_heal": record.get("self_heal")
                    or self.service_health_state.get(name),
                }
            )
        
        # 聚合告警：让现场/云端一眼区分“服务挂了” vs “外部依赖离线（PLC）”
        warnings: list[dict[str, Any]] = []
        for item in services:
            try:
                http_check = (item.get("checks") or {}).get("http") or {}
                rj = http_check.get("response_json") or {}
                if isinstance(rj, dict) and rj.get("reason") == "PLC_OFFLINE":
                    warnings.append(
                        {
                            "type": "PLC_OFFLINE",
                            "service": item.get("name"),
                            "hint": rj.get("hint") or "PLC/Modbus 不通：请检查 PLC 上电/网络/地址配置。",
                        }
                    )
            except (AttributeError, TypeError):
                continue

        control_status = self.control_status or {
            "enabled": self.control_status_enabled,
            "status": "unknown",
            "self_heal": {"enabled": False, "reason": "control_visibility_probe_only"},
        }
        if control_status.get("status") in ("degraded", "down"):
            warnings.append(
                {
                    "type": "CONTROL_VISIBILITY_DEGRADED",
                    "service": control_status.get("name") or "control-status",
                    "mode": control_status.get("mode"),
                    "reason": control_status.get("detail"),
                }
            )
        
        # 组装完整消息
        return {
            "schema": self.status_schema,  # 消息 schema 版本
            "line": self.line_id,           # 产线标识
            "timestamp": int(time.time()),  # 消息时间戳
            "services": services,           # 服务状态列表
            "control": control_status,
            "warnings": warnings,
            "system": {
                "metrics": self.system_metrics,
                "updated_at": self.system_metrics.get("updated_at"),
            },
        }


# ============================================================
# FastAPI 应用配置
# ============================================================

# 全局监控服务实例
monitor_service = MonitorService()

# 创建 FastAPI 应用
app = FastAPI(
    title="Edge Monitor Service",
    description="边缘节点监控服务 - 系统指标采集与服务健康检查",
    version="1.0.0",
)

# 安全基线：edge-monitor 属于“高危运维面”，默认不对浏览器做跨域开放。
# ops-console 建议通过 edge-api 的 /api/v1/ops/* 代理访问（由 edge-api 统一鉴权与审计）。


# ============================================================
# 应用生命周期事件
# ============================================================

@app.on_event("startup")
async def on_startup():
    """应用启动时初始化监控服务"""
    await monitor_service.start()


@app.on_event("shutdown")
async def on_shutdown():
    """应用关闭时停止监控服务"""
    await monitor_service.stop()


# ============================================================
# API 端点
# ============================================================

@app.get("/health")
async def health():
    """
    健康检查端点
    
    供 K8s/Docker 探活使用，返回服务运行状态。
    
    Returns:
        包含服务状态的 JSON 响应
    """
    return {
        "success": True,
        "service": "edge-monitor",
        "status": "running" if monitor_service._running else "stopped",
        "timestamp": int(time.time()),
    }

@app.get("/ready")
async def ready():
    """
    readiness：用于编排系统判断“监控服务是否已进入运行态”。
    不做外部依赖强校验（避免把 Influx/MQTT 抖动误判为 monitor 不可用）。
    """
    ok = bool(monitor_service._running)
    return {
        "ready": ok,
        "service": "edge-monitor",
        "timestamp": int(time.time()),
    }


@app.get("/.well-known/drycontroller/bootstrap.json")
async def discovery_bootstrap():
    payload = monitor_service.discovery_agent.get_payload()
    if not payload:
        raise HTTPException(
            status_code=503,
            detail="discovery agent not ready",
        )
    return payload


@app.get("/system/health")
async def system_health():
    """
    系统健康状态端点
    
    返回完整的系统状态信息，包括：
    - 系统指标（CPU/内存/磁盘）
    - 各服务的健康状态
    
    Returns:
        包含完整状态信息的 JSON 响应
    """
    payload = monitor_service._build_status_payload()
    payload["success"] = True
    return payload


@app.get("/system/metrics")
async def system_metrics():
    """
    系统指标端点

    返回最新的系统指标数据。

    Returns:
        包含 CPU、内存、磁盘等指标的 JSON 响应
    """
    return {
        "success": True,
        "line": monitor_service.line_id,
        "metrics": monitor_service.system_metrics,
        "timestamp": int(time.time()),
    }


@app.get("/metrics")
async def prometheus_metrics():
    # Prometheus exposition format. /system/metrics above stays as-is
    # (JSON, business-facing); this is the standard scrape target.
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/ops/self-heal/restart")
async def manual_self_heal_restart(req: SelfHealRestartRequest, _: str = Depends(require_ops_api_key)):
    result = await monitor_service.manual_restart(req.service)
    return result


@app.post("/ops/self-heal/toggle")
async def manual_self_heal_toggle(req: SelfHealToggleRequest, _: str = Depends(require_ops_api_key)):
    return monitor_service.set_self_heal_enabled(req.service, req.enabled)


@app.get("/ops/logs/{service}")
async def fetch_service_logs(
    service: str,
    lines: int = Query(200, ge=1, le=2000),
    _: str = Depends(require_ops_api_key),
):
    try:
        data = monitor_service.read_service_logs(service, lines)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="log_file_not_found")
    except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - 文件 IO
        logger.error("log_read_failed", service=service, error=str(exc))
        raise HTTPException(status_code=500, detail="log_read_failed")
    return {"success": True, "service": service, "count": len(data), "data": data}


@app.get("/ops/alerts")
async def list_alerts(
    severity: str | None = Query(None, description="过滤 severity，如 critical/warning"),
    service: str | None = Query(None, description="过滤 service 标签"),
    state: str | None = Query("active", description="active/silenced/inhibited"),
    _: str = Depends(require_ops_api_key),
):
    """
    代理 Alertmanager，提供运维告警聚合给 ops-console 使用。
    需要配置 ALERTMANAGER_BASE_URL，支持 basic auth。
    """
    alerts = await monitor_service._fetch_alertmanager_alerts(severity=severity, service=service, state=state)
    return {"success": True, "count": len(alerts), "alerts": alerts}


# ============================================================
# 主函数入口
# ============================================================

if __name__ == "__main__":
    import uvicorn
    
    # 使用 uvicorn 启动 FastAPI 应用
    uvicorn.run(
        "monitor.main:app",
        host="0.0.0.0",
        port=8080,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
