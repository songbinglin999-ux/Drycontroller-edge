"""
应用配置管理
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List
import os


class Settings(BaseSettings):
    """应用配置"""
    
    # 应用信息
    APP_NAME: str = "Edge Backend API"
    VERSION: str = "1.0.0"
    DEBUG: bool = False
    ENV: str = "production"
    
    # 日志配置
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "/app/logs/edge-api.log"
    
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
    
    # MQTT 配置
    MQTT_BROKER: str = "localhost"
    MQTT_PORT: int = 1883
    MQTT_USER: str = "edge_mqtt"
    MQTT_PASSWORD: str = "mqtt_password"
    MQTT_CLIENT_ID: str = "edge-api"
    
    # API 配置
    API_PREFIX: str = "/api/v1"
    CORS_ORIGINS: List[str] = ["*"]
    
    # JWT 配置
    SECRET_KEY: str = "your-secret-key-here-change-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours
    
    # 限流配置
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MINUTE: int = 60
    
    # 配置文件路径
    CONFIG_DIR: str = "/app/configs"
    TAGS_CONFIG: str = "/app/configs/tags.yaml"
    AI_CONFIG: str = "/app/configs/ai-config.yaml"
    QOS_CONFIG: str = "/app/configs/qos.yaml"
    LIMITS_CONFIG: str = "/app/configs/limits.yaml"
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )
    
    @property
    def database_url(self) -> str:
        """获取数据库连接字符串"""
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
    
    @property
    def influxdb_url(self) -> str:
        """获取 InfluxDB 连接字符串"""
        return f"http://{self.INFLUXDB_HOST}:{self.INFLUXDB_PORT}"


# 创建全局配置实例
settings = Settings()

