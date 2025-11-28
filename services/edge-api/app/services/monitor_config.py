"""
Monitor 配置管理服务。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Any, List, Optional

import yaml

from app.core.config import settings


class MonitorConfigService:
    """
    负责读取 / 更新 configs/monitor.yaml。
    """

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = Path(config_path or settings.MONITOR_CONFIG)

    def load(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            raise FileNotFoundError(f"monitor config not found: {self.config_path}")
        with self.config_path.open("r", encoding="utf-8") as fp:
            return yaml.safe_load(fp) or {}

    def save(self, data: Dict[str, Any]) -> Dict[str, Any]:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config_path.open("w", encoding="utf-8") as fp:
            yaml.safe_dump(data, fp, allow_unicode=True, sort_keys=False)
        return data

    def patch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = self.load()
        services_cfg = data.setdefault("services", {})

        for field in ["interval", "http_timeout", "failure_threshold", "recovery_observe", "max_restart_per_hour"]:
            if field in payload and payload[field] is not None:
                services_cfg[field] = payload[field]

        if "self_heal_enabled" in payload and services_cfg.get("self_heal") is not None:
            services_cfg["self_heal"]["enabled"] = bool(payload["self_heal_enabled"])
        elif "self_heal_enabled" in payload:
            services_cfg["self_heal"] = {"enabled": bool(payload["self_heal_enabled"])}

        if "targets" in payload:
            target_updates: List[Dict[str, Any]] = payload["targets"]
            service_targets = services_cfg.setdefault("targets", [])
            service_index = {target.get("name"): target for target in service_targets}
            for update in target_updates:
                name = update.get("name")
                if not name:
                    continue
                target_cfg = service_index.get(name)
                if not target_cfg:
                    target_cfg = {"name": name}
                    service_targets.append(target_cfg)
                    service_index[name] = target_cfg
                if "self_heal" in update:
                    target_cfg["self_heal"] = {**(target_cfg.get("self_heal") or {}), **update["self_heal"]}

        return self.save(data)


_monitor_config_service: MonitorConfigService | None = None


def get_monitor_config_service() -> MonitorConfigService:
    global _monitor_config_service
    if not _monitor_config_service:
        _monitor_config_service = MonitorConfigService()
    return _monitor_config_service

