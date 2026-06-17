"""
Discovery Agent

负责在边缘站点内生成统一的 bootstrap 配置，供前端自动发现 edge-api。

核心能力：
- 周期性生成 `.well-known/drycontroller/bootstrap.json`
- 暴露最新 payload 供 FastAPI 路由直接返回
- 可选 HMAC-SHA256 签名，防止被篡改
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import socket
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, cast

import structlog
from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf


def _to_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


logger = structlog.get_logger(__name__)


class DiscoveryAgent:
    """管理 bootstrap 配置生成/缓存/签名。"""

    def __init__(self, line_id: str):
        self.enabled = _to_bool(os.getenv("DISCOVERY_AGENT_ENABLED", "true"))
        self.line_id = line_id
        self.api_base = os.getenv("DISCOVERY_API_BASE", "http://edge-api:8000/api").rstrip("/")
        self.ws_base = os.getenv("DISCOVERY_WS_BASE", "ws://edge-api:8000/api").rstrip("/")
        self.hostname = os.getenv("DISCOVERY_HOSTNAME", os.getenv("HOSTNAME", "edge-node"))
        self.version = os.getenv("DISCOVERY_VERSION", "bootstrap.v1")
        self.publish_interval = int(os.getenv("DISCOVERY_PUBLISH_INTERVAL", "15"))
        self.ttl_seconds = int(os.getenv("DISCOVERY_BOOTSTRAP_TTL", "30"))
        self.bootstrap_path = Path(os.getenv("DISCOVERY_BOOTSTRAP_PATH", "/app/bootstrap/bootstrap.json"))
        self.signing_key = os.getenv("DISCOVERY_SIGNING_KEY")
        self.extra_fields = self._parse_extra(os.getenv("DISCOVERY_EXTRA_FIELDS"))
        self.mqtt_topic = os.getenv("DISCOVERY_MQTT_TOPIC", "system/bootstrap")
        self.mqtt_retain = _to_bool(os.getenv("DISCOVERY_MQTT_RETAIN", "true"))
        self._mqtt_client_supplier: Optional[Callable[[], Any]] = None

        self.mdns_enabled = _to_bool(os.getenv("DISCOVERY_MDNS_ENABLED", "true"))
        self.mdns_port = int(os.getenv("DISCOVERY_MDNS_PORT", "8080"))
        self.mdns_service_type = os.getenv("DISCOVERY_MDNS_SERVICE_TYPE", "_dry-edge._tcp.local.")
        raw_name = os.getenv("DISCOVERY_MDNS_SERVICE_NAME", f"{self.hostname}._dry-edge._tcp.local.")
        self.mdns_service_name = raw_name if raw_name.endswith(".local.") else f"{raw_name}.local."
        self.mdns_host_override = os.getenv("DISCOVERY_MDNS_HOST")
        server_default = os.getenv("DISCOVERY_MDNS_SERVER", f"{self.hostname}.local.")
        self.mdns_server = server_default if server_default.endswith(".local.") else f"{server_default}.local."

        self._mdns: Optional[AsyncZeroconf] = None
        self._mdns_info: Optional[ServiceInfo] = None

        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._latest_payload: Optional[Dict[str, Any]] = None

    async def start(self):
        if not self.enabled:
            logger.info("discovery_agent_disabled")
            return
        self._running = True
        await self._publish()
        self._task = asyncio.create_task(self._loop())
        logger.info("discovery_agent_started", api=self.api_base, ws=self.ws_base)

    async def stop(self):
        if not self.enabled:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._stop_mdns()
        logger.info("discovery_agent_stopped")

    def get_payload(self) -> Optional[Dict[str, Any]]:
        """返回最近一次生成的 payload。"""
        return self._latest_payload

    async def _loop(self):
        while self._running:
            await asyncio.sleep(self.publish_interval)
            await self._publish()

    async def _publish(self):
        payload = self._build_payload()
        self._latest_payload = payload
        self._write_bootstrap_file(payload)
        await self._ensure_mdns(payload)
        self._publish_mqtt(payload)

    def _build_payload(self) -> Dict[str, Any]:
        now = int(time.time())
        payload = {
            "version": self.version,
            "line_id": self.line_id,
            "hostname": self.hostname,
            "api_base_url": self.api_base,
            "ws_base_url": self.ws_base,
            "generated_at": now,
            "expires_at": now + self.ttl_seconds,
            "nonce": secrets.token_hex(8),
        }
        if self.extra_fields:
            payload["extra"] = self.extra_fields
        if self.signing_key:
            payload["signature"] = self._sign_payload(payload)
        return payload

    def _write_bootstrap_file(self, payload: Dict[str, Any]):
        try:
            self.bootstrap_path.parent.mkdir(parents=True, exist_ok=True)
            self.bootstrap_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, TypeError, ValueError) as exc:
            logger.error("discovery_bootstrap_write_failed", path=str(self.bootstrap_path), error=str(exc))

    def _sign_payload(self, payload: Dict[str, Any]) -> str:
        if not self.signing_key:
            raise ValueError("signing_key is required to sign payloads")
        body = json.dumps(
            {k: payload[k] for k in sorted(payload) if k != "signature"},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        digest = hmac.new(self.signing_key.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return digest

    @staticmethod
    def _parse_extra(raw: Optional[str]) -> Optional[Dict[str, Any]]:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("discovery_extra_fields_invalid")
            return None

    def set_mqtt_client_supplier(self, supplier: Callable[[], Any]):
        """由 MonitorService 注入 MQTT 客户端获取函数。"""
        self._mqtt_client_supplier = supplier

    async def _ensure_mdns(self, payload: Dict[str, Any]):
        if not self.mdns_enabled:
            return
        if not self._mdns:
            await self._register_mdns(payload)
        else:
            await self._update_mdns(payload)

    async def _register_mdns(self, payload: Dict[str, Any]):
        try:
            address = self._resolve_mdns_address()
            if not address:
                return
            info = ServiceInfo(
                type_=self.mdns_service_type,
                name=self.mdns_service_name,
                port=self.mdns_port,
                addresses=[address],
                server=self.mdns_server,
                properties=self._build_mdns_properties(payload),
            )
            self._mdns = AsyncZeroconf()
            await self._mdns.async_register_service(info)
            self._mdns_info = info
            logger.info("mdns_service_registered", service=self.mdns_service_name, port=self.mdns_port)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("mdns_register_failed", error=str(exc), exception=repr(exc))
            self._mdns = None
            self._mdns_info = None

    async def _update_mdns(self, payload: Dict[str, Any]):
        mdns = self._mdns
        info = self._mdns_info
        if not mdns or not info:
            await self._register_mdns(payload)
            return
        try:
            updated_info = ServiceInfo(
                type_=self.mdns_service_type,
                name=self.mdns_service_name,
                port=self.mdns_port,
                addresses=info.addresses,
                server=self.mdns_server,
                properties=self._build_mdns_properties(payload),
            )
            await mdns.async_update_service(updated_info)
            self._mdns_info = updated_info
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("mdns_update_failed", error=str(exc), exception=repr(exc))

    def _build_mdns_properties(self, payload: Dict[str, Any]) -> Dict[bytes, bytes]:
        api_value = cast(str, payload.get("api_base_url", self.api_base) or self.api_base)
        ws_value = cast(str, payload.get("ws_base_url", self.ws_base) or self.ws_base)
        line_value = cast(str, payload.get("line_id", self.line_id) or self.line_id)
        props: Dict[bytes, bytes] = {
            b"api": api_value.encode("utf-8"),
            b"ws": ws_value.encode("utf-8"),
            b"line": line_value.encode("utf-8"),
        }
        version_value = payload.get("version")
        if version_value is not None:
            props[b"v"] = str(version_value).encode("utf-8")
        return props

    def _resolve_mdns_address(self) -> Optional[bytes]:
        host = self.mdns_host_override
        if not host:
            try:
                host = socket.gethostbyname(socket.gethostname())
            except socket.gaierror as exc:
                logger.warning("mdns_resolve_failed", error=str(exc))
                return None
        try:
            return socket.inet_aton(host)
        except OSError as exc:
            logger.warning("mdns_invalid_host", host=host, error=str(exc))
            return None

    async def _stop_mdns(self):
        mdns = self._mdns
        if not mdns:
            return
        try:
            info = self._mdns_info
            if info:
                await mdns.async_unregister_service(info)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("mdns_unregister_failed", error=str(exc), exception=repr(exc))
        finally:
            try:
                await mdns.async_close()
            except (OSError, RuntimeError, ValueError):
                pass
            self._mdns = None
            self._mdns_info = None

    def _publish_mqtt(self, payload: Dict[str, Any]):
        if not self._mqtt_client_supplier:
            return
        client = None
        try:
            client = self._mqtt_client_supplier()
        except (AttributeError, RuntimeError):
            client = None
        if not client:
            return
        try:
            client.publish(
                self.mqtt_topic,
                json.dumps(payload),
                qos=1,
                retain=self.mqtt_retain,
            )
            logger.debug(
                "bootstrap_mqtt_published",
                topic=self.mqtt_topic,
                retain=self.mqtt_retain,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning(
                "bootstrap_mqtt_publish_failed",
                error=str(exc),
                topic=self.mqtt_topic,
            )
