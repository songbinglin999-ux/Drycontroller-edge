"""
应用配置管理
"""

from pathlib import Path
import os
import secrets
from typing import List

from pydantic import field_validator, model_validator
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
    CORS_ORIGINS: List[str] = ["*"]
    CORS_ALLOW_CREDENTIALS: bool = False
    
    # JWT 配置
    SECRET_KEY: str = ""
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours
    SECRET_KEY_FILE: str | None = None

    # API Key 鉴权
    API_KEY_HEADER: str = "X-API-Key"
    CONTROL_API_KEYS: List[str] = []
    ADMIN_API_KEYS: List[str] = []
    
    # 限流配置
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MINUTE: int = 60
    
    # 配置文件路径
    CONFIG_DIR: str = "/app/configs"
    TAGS_CONFIG: str = "/app/configs/tags.yaml"
    AI_CONFIG: str = "/app/configs/ai-config.yaml"
    QOS_CONFIG: str = "/app/configs/qos.yaml"
    LIMITS_CONFIG: str = "/app/configs/limits.yaml"
    MONITOR_CONFIG: str = "/app/configs/monitor.yaml"
    MONITOR_API_BASE: str = "http://edge-monitor:8080"
    
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

    @field_validator("CONTROL_API_KEYS", "ADMIN_API_KEYS", mode="before")
    @classmethod
    def _split_api_keys(cls, value):
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value or []

    @model_validator(mode="after")
    def _ensure_secret_key(self):
        secret = self.SECRET_KEY.strip() if isinstance(self.SECRET_KEY, str) else ""
        if secret:
            return self

        if self.SECRET_KEY_FILE:
            candidate = Path(self.SECRET_KEY_FILE)
            if candidate.exists():
                self.SECRET_KEY = candidate.read_text(encoding="utf-8").strip()
                if self.SECRET_KEY:
                    return self

        self.SECRET_KEY = secrets.token_urlsafe(32)
        return self


# 创建全局配置实例
settings = Settings()

