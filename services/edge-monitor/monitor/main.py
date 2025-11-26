"""
系统监控服务
"""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

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
                        "status": "error",
                        "detail": str(result),
                        "timestamp": now,
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
        uptime = int(time.time() - psutil.boot_time())
        
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
        }
        logger.debug("system_metrics_collected", **metrics)
        return metrics
    
    async def _check_service(self, target: Dict[str, Any]) -> Dict[str, Any]:
        name = target.get("name", "unknown")
        service_status = {
            "status": "unknown",
            "timestamp": int(time.time()),
        }
        url = target.get("url")
        container_name = target.get("container")
        
        if url and self.http_client:
            try:
                start = time.perf_counter()
                response = await self.http_client.get(url)
                latency_ms = (time.perf_counter() - start) * 1000
                service_status["latency_ms"] = round(latency_ms, 2)
                if response.status_code == 200:
                    service_status["status"] = "healthy"
                else:
                    service_status["status"] = "degraded"
                    service_status["detail"] = f"http {response.status_code}"
            except Exception as exc:
                service_status["status"] = "unreachable"
                service_status["detail"] = str(exc)
        
        if container_name and self.docker_client:
            try:
                container = self.docker_client.containers.get(container_name)
                service_status["container_state"] = container.status
                if service_status["status"] == "unknown":
                    service_status["status"] = "healthy" if container.status == "running" else "degraded"
            except docker_errors.NotFound:
                service_status["container_state"] = "missing"
                service_status["status"] = "down"
            except docker_errors.DockerException as exc:
                service_status["detail"] = f"docker:{exc}"
                service_status.setdefault("container_state", "error")
        
        logger.info("service_check", service=name, status=service_status["status"])
        return service_status
    
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
                latency = status.get("latency_ms")
                container_state = status.get("container_state", "")
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
        payload = {
            "line": self.line_id,
            "ts": int(time.time()),
            "services": self.service_status,
            "system": self.system_metrics,
        }
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
    return {
        "success": True,
        "line": monitor_service.line_id,
        "services": monitor_service.service_status,
        "timestamp": int(time.time()),
    }


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

