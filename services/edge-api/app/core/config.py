"""
应用配置管理
"""

import os
import json
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置"""
    
    # 应用信息
    APP_NAME: str = "Edge Backend API"
    VERSION: str = "1.0.0"
    DEBUG: bool = False
    ENV: str = "production"
    
    # 日志配置
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "/logs/edge-api"
    LOG_FILE: str = "/logs/edge-api/app.log"
    
    # 数据库配置
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "edge_user"
    POSTGRES_PASSWORD: str = "edge_password"
    POSTGRES_DB: str = "edge_db"
    
    # InfluxDB 配置
    INFLUXDB_HOST: str = "localhost"
    INFLUXDB_PORT: int = 8086
    INFLUXDB_TOKEN: str = ""
    INFLUXDB_ORG: str = "edge-org"
    INFLUXDB_BUCKET: str = "industrial-data"
    TRAINING_MEASUREMENT: str = "softsensor_training_data"
    SYSTEM_METRICS_MEASUREMENT: str = "system_metrics"
    SYSTEM_HEALTH_MEASUREMENT: str = "system_health"
    
    # MQTT 配置
    MQTT_BROKER: str = "localhost"
    MQTT_PORT: int = 1883
    MQTT_USER: str = "edge_mqtt"
    MQTT_PASSWORD: str = "mqtt_password"
    MQTT_CLIENT_ID: str = "edge-api"
    
    # API 配置
    API_PREFIX: str = "/api/v1"
    # 生产默认最小暴露：不开放任意跨域。需要时在 .env 显式配置（支持 JSON 数组或逗号分隔）。
    CORS_ORIGINS: List[str] = []
    CORS_ALLOW_CREDENTIALS: bool = False

    # API Key 鉴权
    API_KEY_HEADER: str = "X-API-Key"
    CONTROL_API_KEYS: List[str] = []
    ADMIN_API_KEYS: List[str] = []
    
    # 配置文件路径
    CONFIG_DIR: str = "/app/configs"
    TAGS_CONFIG: str = "/app/configs/tags.yaml"
    AI_CONFIG: str = "/app/configs/ai-config.yaml"
    QOS_CONFIG: str = "/app/configs/qos.yaml"
    LIMITS_CONFIG: str = "/app/configs/limits.yaml"
    MONITOR_CONFIG: str = "/app/configs/monitor.yaml"
    MONITOR_API_BASE: str = "http://edge-monitor:8080"

    # License 安装与校验：edge-api 负责受控写入，license-service 负责验签判定
    LICENSE_FILE_PATH: str = "/etc/drycontroller/license/license.json"
    LICENSE_PUBLIC_KEY_PATH: str = "/etc/drycontroller/keys/license.pub"
    # 可选信任锚。为空时 edge-api 使用 LICENSE_PUBLIC_KEY_PATH 已存在的公钥作为信任锚。
    LICENSE_TRUSTED_PUBLIC_KEY: str = ""
    LICENSE_SERVICE_BASE: str = "http://license-service:8000"
    
    # OTA Agent（执行面，建议仅由 edge-api 代理调用）
    OTA_AGENT_API_BASE: str = "http://edge-ota-agent:8090"

    # Cloud sync outbox：edge 本地 audit 等事件先写 cloud_sync_outbox，再后台推送 cloud-ops。
    CLOUD_SYNC_OUTBOX_ENABLED: bool = True
    CLOUD_SYNC_OUTBOX_INTERVAL_SEC: float = 5.0
    CLOUD_SYNC_OUTBOX_BATCH_SIZE: int = 50
    CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS: int = 10
    CLOUD_SYNC_OUTBOX_AUDIT_PATH: str = "/api/v1/audit/from-edge"
    # cloud-ops 端 header 名（默认 X-Enroll-Token，与 cloudserver settings.ENROLL_TOKEN_HEADER 对齐）
    CLOUD_SYNC_OUTBOX_ENROLL_TOKEN_HEADER: str = "X-Enroll-Token"
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="allow",
    )
    
    @property
    def database_url(self) -> str:
        """获取数据库连接字符串"""
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
    
    @property
    def influxdb_url(self) -> str:
        """获取 InfluxDB 连接字符串"""
        return f"http://{self.INFLUXDB_HOST}:{self.INFLUXDB_PORT}"

    @property
    def api_security_enabled(self) -> bool:
        return bool(self.CONTROL_API_KEYS or self.ADMIN_API_KEYS)

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value):
        """
        兼容两种写法：
        - JSON 数组：["https://a.com","https://b.com"]
        - 逗号分隔：https://a.com,https://b.com
        """
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        return [str(v).strip() for v in parsed if str(v).strip()]
                except Exception:
                    # 回退到逗号分隔
                    pass
            return [item.strip() for item in raw.split(",") if item.strip()]
        return []

    @field_validator("CONTROL_API_KEYS", "ADMIN_API_KEYS", mode="before")
    @classmethod
    def _split_api_keys(cls, value):
        """
        兼容两种写法：
        - JSON 数组：["k1","k2"]
        - 逗号分隔：k1,k2
        """
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        return [str(v).strip() for v in parsed if str(v).strip()]
                except Exception:
                    pass
            return [item.strip() for item in raw.split(",") if item.strip()]
        return value or []


# 创建全局配置实例
settings = Settings()
