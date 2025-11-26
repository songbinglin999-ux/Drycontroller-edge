"""
系统监控服务
"""

import asyncio
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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import ASYNCHRONOUS
import paho.mqtt.client as mqtt

from common.logging import setup_logging

event_recorder = setup_logging("edge-monitor")
logger = structlog.get_logger(__name__)

DEFAULT_CONFIG_PATH = Path(os.getenv("MONITOR_CONFIG", "/app/configs/monitor.yaml"))
STATUS_PRIORITY = {
    "unknown": 0,
    "healthy": 1,
    "degraded": 2,
    "down": 3,
}


class MonitorService:
    """负责系统指标采集、服务探活与状态发布"""
    
    def __init__(self):
        self.config = self._load_config()
        self.line_id = self.config.get("line_id") or os.getenv("LINE_ID", "L1")
        
        system_cfg = self.config.get("system", {})
        service_cfg = self.config.get("services", {})
        
        self.system_interval = int(system_cfg.get("interval", 30))
        self.service_interval = int(service_cfg.get("interval", 20))
        self.disk_path = system_cfg.get("disk_path", "/")
        self.service_targets: List[Dict[str, Any]] = service_cfg.get("targets", [])
        self.http_timeout = float(service_cfg.get("http_timeout", 5))
        self.publish_mqtt = bool(system_cfg.get("publish_mqtt", True))
        
        self.mqtt_broker = os.getenv("MQTT_BROKER", "localhost")
        self.mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
        self.mqtt_user = os.getenv("MQTT_USER", "")
        self.mqtt_password = os.getenv("MQTT_PASSWORD", "")
        self.status_topic = os.getenv(
            "MQTT_TOPIC_STATUS",
            f"system/{self.line_id}/status",
        )
        
        self.influx_host = os.getenv("INFLUXDB_HOST", "localhost")
        self.influx_port = os.getenv("INFLUXDB_PORT", "8086")
        self.influx_token = os.getenv("INFLUXDB_TOKEN", "")
        self.influx_org = os.getenv("INFLUXDB_ORG", "edge-org")
        self.influx_bucket = os.getenv("INFLUXDB_BUCKET", "industrial-data")
        self.system_measurement = os.getenv("MONITOR_SYSTEM_MEASUREMENT", "system_metrics")
        self.service_measurement = os.getenv("MONITOR_SERVICE_MEASUREMENT", "system_health")
        self.status_schema = "edge.monitor.status.v1"
        
        self.mqtt_client = None
        self.influx_client = None
        self.write_api = None
        self.http_client: httpx.AsyncClient | None = None
        self.docker_client = None
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self.system_metrics: Dict[str, Any] = {}
        self.service_status: Dict[str, Any] = {}
        
        psutil.cpu_percent(interval=None)
    
    async def start(self):
        logger.info("edge_monitor_starting", line=self.line_id)
        event_recorder.record_event(
            "service_starting",
            tags={"component": "edge-monitor", "line": self.line_id},
        )
        self._connect_mqtt()
        self._connect_influx()
        await self._connect_http()
        self._connect_docker()
        
        self._running = True
        self._tasks = [
            asyncio.create_task(self._system_metrics_loop()),
            asyncio.create_task(self._service_health_loop()),
        ]
        logger.info("edge_monitor_started")
        event_recorder.record_event(
            "service_started",
            tags={"component": "edge-monitor", "line": self.line_id},
        )
    
    async def stop(self):
        logger.info("edge_monitor_stopping")
        event_recorder.record_event(
            "service_stopping",
            tags={"component": "edge-monitor", "line": self.line_id},
        )
        self._running = False
        
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        
        if self.http_client:
            await self.http_client.aclose()
            self.http_client = None
        
        if self.write_api and self.influx_client:
            self.influx_client.close()
            self.write_api = None
        
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            self.mqtt_client = None
        
        if self.docker_client:
            self.docker_client.close()
            self.docker_client = None
        
        logger.info("edge_monitor_stopped")
        event_recorder.record_event(
            "service_stopped",
            tags={"component": "edge-monitor", "line": self.line_id},
        )
    
    def _load_config(self) -> Dict[str, Any]:
        if DEFAULT_CONFIG_PATH.exists():
            with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        logger.warning("monitor_config_missing", path=str(DEFAULT_CONFIG_PATH))
        return {}
    
    def _connect_mqtt(self):
        try:
            self.mqtt_client = mqtt.Client(client_id="edge-monitor")
            if self.mqtt_user and self.mqtt_password:
                self.mqtt_client.username_pw_set(self.mqtt_user, self.mqtt_password)
            self.mqtt_client.connect(self.mqtt_broker, self.mqtt_port, keepalive=60)
            self.mqtt_client.loop_start()
            logger.info("monitor_mqtt_connected", broker=self.mqtt_broker, port=self.mqtt_port)
        except Exception as exc:
            logger.error("monitor_mqtt_connect_failed", error=str(exc))
            self.mqtt_client = None
    
    def _connect_influx(self):
        try:
            url = f"http://{self.influx_host}:{self.influx_port}"
            self.influx_client = InfluxDBClient(
                url=url,
                token=self.influx_token,
                org=self.influx_org,
            )
            self.write_api = self.influx_client.write_api(write_options=ASYNCHRONOUS)
            logger.info("monitor_influx_connected", url=url, bucket=self.influx_bucket)
        except Exception as exc:
            logger.error("monitor_influx_connect_failed", error=str(exc))
            self.influx_client = None
            self.write_api = None
    
    async def _connect_http(self):
        self.http_client = httpx.AsyncClient(timeout=self.http_timeout)
    
    def _connect_docker(self):
        try:
            self.docker_client = docker.from_env()
        except docker_errors.DockerException as exc:
            logger.error("monitor_docker_connect_failed", error=str(exc))
            self.docker_client = None
    
    async def _system_metrics_loop(self):
        while self._running:
            metrics = self._collect_system_metrics()
            self.system_metrics = metrics
            self._write_system_metrics(metrics)
            if self.publish_mqtt:
                self._publish_status()
            await asyncio.sleep(self.system_interval)
    
    async def _service_health_loop(self):
        while self._running:
            tasks = [self._check_service(target) for target in self.service_targets]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            now = int(time.time())
            aggregated = {}
            for target, result in zip(self.service_targets, results):
                name = target.get("name", "unknown")
                if isinstance(result, dict):
                    aggregated[name] = result
                else:
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
            self.service_status = aggregated
            self._write_service_health(aggregated)
            if self.publish_mqtt:
                self._publish_status()
            await asyncio.sleep(self.service_interval)
    
    def _collect_system_metrics(self) -> Dict[str, Any]:
        cpu_percent = psutil.cpu_percent(interval=0.1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(self.disk_path)
        load_avg = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
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
        name = target.get("name", "unknown")
        timestamp = int(time.time())
        overall_status = "unknown"
        checks: Dict[str, Any] = {}
        
        http_cfg = target.get("http", {})
        http_url = http_cfg.get("url") or target.get("url")
        if http_url:
            http_result = await self._http_health_check(http_url)
            checks["http"] = http_result
            overall_status = self._merge_status(overall_status, http_result.get("status"))
        
        tcp_cfg = target.get("tcp")
        if tcp_cfg:
            tcp_result = await self._tcp_health_check(
                host=tcp_cfg.get("host") or name,
                port=tcp_cfg.get("port"),
            )
            checks["tcp"] = tcp_result
            overall_status = self._merge_status(overall_status, tcp_result.get("status"))
        
        container_name = target.get("container")
        if container_name:
            container_result = self._container_health_check(container_name)
            checks["container"] = container_result
            overall_status = self._merge_status(overall_status, container_result.get("status"))
        
        if not checks:
            checks["monitor"] = {
                "status": "degraded",
                "detail": "no_checks_configured",
            }
        
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
    
    async def _http_health_check(self, url: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {"status": "unknown", "url": url}
        if not self.http_client:
            result["status"] = "degraded"
            result["detail"] = "http_client_not_ready"
            return result
        try:
            start = time.perf_counter()
            response = await self.http_client.get(url)
            latency_ms = (time.perf_counter() - start) * 1000
            result["latency_ms"] = round(latency_ms, 2)
            result["status_code"] = response.status_code
            if 200 <= response.status_code < 300:
                result["status"] = "healthy"
            else:
                result["status"] = "degraded"
                result["detail"] = f"http {response.status_code}"
        except Exception as exc:
            result["status"] = "down"
            result["detail"] = str(exc)
        return result
    
    async def _tcp_health_check(self, host: Optional[str], port: Optional[int]) -> Dict[str, Any]:
        result: Dict[str, Any] = {"status": "unknown", "host": host, "port": port}
        if not host or port is None:
            result["status"] = "degraded"
            result["detail"] = "tcp_host_or_port_missing"
            return result
        try:
            port_int = int(port)
            start = time.perf_counter()
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port_int),
                timeout=self.http_timeout,
            )
            writer.close()
            if hasattr(writer, "wait_closed"):
                await writer.wait_closed()
            latency_ms = (time.perf_counter() - start) * 1000
            result["latency_ms"] = round(latency_ms, 2)
            result["status"] = "healthy"
        except Exception as exc:
            result["status"] = "down"
            result["detail"] = str(exc)
        return result
    
    def _container_health_check(self, container_name: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {"status": "unknown", "name": container_name}
        if not self.docker_client:
            result["status"] = "degraded"
            result["detail"] = "docker_client_not_ready"
            return result
        try:
            container = self.docker_client.containers.get(container_name)
            result["state"] = container.status
            result["status"] = "healthy" if container.status == "running" else "degraded"
        except docker_errors.NotFound:
            result["state"] = "missing"
            result["status"] = "down"
            result["detail"] = "container_not_found"
        except docker_errors.DockerException as exc:
            result["status"] = "degraded"
            result["detail"] = f"docker:{exc}"
        return result
    
    def _merge_status(self, current: str, candidate: Optional[str]) -> str:
        if not candidate:
            return current
        if STATUS_PRIORITY.get(candidate, 0) > STATUS_PRIORITY.get(current, 0):
            return candidate
        return current
    
    def _write_system_metrics(self, metrics: Dict[str, Any]):
        if not self.write_api:
            return
        try:
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
                .time(int(time.time() * 1e9))
            )
            self.write_api.write(self.influx_bucket, self.influx_org, point)
        except Exception as exc:
            logger.error("system_metrics_write_failed", error=str(exc))
            event_recorder.record_event(
                "system_metrics_write_failed",
                severity="error",
                fields={"error": str(exc)},
                tags={"component": "edge-monitor"},
            )
    
    def _write_service_health(self, statuses: Dict[str, Any]):
        if not self.write_api:
            return
        points = []
        now = int(time.time() * 1e9)
        try:
            for name, status in statuses.items():
                state = status.get("status", "unknown")
                latency = status.get("latency_ms") or status.get("checks", {}).get("http", {}).get("latency_ms")
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
            if points:
                self.write_api.write(self.influx_bucket, self.influx_org, points)
        except Exception as exc:
            logger.error("service_health_write_failed", error=str(exc))
            event_recorder.record_event(
                "service_health_write_failed",
                severity="error",
                fields={"error": str(exc)},
                tags={"component": "edge-monitor"},
            )
    
    def _publish_status(self):
        if not self.mqtt_client:
            return
        payload = self._build_status_payload()
        try:
            self.mqtt_client.publish(self.status_topic, json.dumps(payload), qos=1)
        except Exception as exc:
            logger.error("status_publish_failed", error=str(exc))
            event_recorder.record_event(
                "status_publish_failed",
                severity="error",
                fields={"error": str(exc)},
                tags={"component": "edge-monitor"},
            )

    def _build_status_payload(self) -> Dict[str, Any]:
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
                }
            )
        return {
            "schema": self.status_schema,
            "line": self.line_id,
            "timestamp": int(time.time()),
            "services": services,
            "system": {
                "metrics": self.system_metrics,
                "updated_at": self.system_metrics.get("updated_at"),
            },
        }


monitor_service = MonitorService()

app = FastAPI(
    title="Edge Monitor Service",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    await monitor_service.start()


@app.on_event("shutdown")
async def on_shutdown():
    await monitor_service.stop()


@app.get("/health")
async def health():
    return {
        "success": True,
        "service": "edge-monitor",
        "status": "running" if monitor_service._running else "stopped",
        "timestamp": int(time.time()),
    }


@app.get("/system/health")
async def system_health():
    payload = monitor_service._build_status_payload()
    payload["success"] = True
    return payload


@app.get("/system/metrics")
async def system_metrics():
    return {
        "success": True,
        "line": monitor_service.line_id,
        "metrics": monitor_service.system_metrics,
        "timestamp": int(time.time()),
    }


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "monitor.main:app",
        host="0.0.0.0",
        port=8080,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )

