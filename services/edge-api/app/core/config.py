"""
应用配置管理
"""

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
    POSTGRES_PASSWORD: str = ""
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
    MQTT_PASSWORD: str = ""
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
    READONLY_WS_ALLOWLIST: List[str] = [
        "127.0.0.1/32",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ]
    
    # 配置文件路径
    CONFIG_DIR: str = "/app/configs"
    TAGS_CONFIG: str = "/app/configs/tags.yaml"
    AI_CONFIG: str = "/app/configs/ai-config.yaml"
    QOS_CONFIG: str = "/app/configs/qos.yaml"
    LIMITS_CONFIG: str = "/app/configs/limits.yaml"
    MONITOR_CONFIG: str = "/app/configs/monitor.yaml"
    MONITOR_API_BASE: str = "http://edge-monitor:8080"

    # License 只读校验：成对激活只由宿主 root 事务管理器执行
    LICENSE_FILE_PATH: str = "/etc/drycontroller/license/license.json"
    LICENSE_TRUST_POLICY_PATH: str = "/etc/drycontroller/keys/license-trust/policy.json"
    LICENSE_SERVICE_BASE: str = "http://license-service:8000"
    
    # OTA Agent（执行面，建议仅由 edge-api 代理调用）
    REGISTRY_DOCKER_CONFIG_PATH: str = "/etc/drycontroller/registry/config.json"
    REGISTRY_CREDENTIAL_STATUS_PATH: str = "/etc/drycontroller/registry/status.json"
    REGISTRY_CREDENTIAL_MAX_AGE_HOURS: float = 0.0

    # Cloud sync outbox：edge 本地 audit 等事件先写 cloud_sync_outbox，再后台推送 cloud-ops。
    CLOUD_SYNC_OUTBOX_ENABLED: bool = True
    CLOUD_SYNC_OUTBOX_INTERVAL_SEC: float = 5.0
    CLOUD_SYNC_OUTBOX_BATCH_SIZE: int = 50
    CLOUD_SYNC_OUTBOX_MAX_ATTEMPTS: int = 10
    CLOUD_SYNC_OUTBOX_AUDIT_PATH: str = "/api/v1/audit/from-edge"
    CLOUD_SYNC_OUTBOX_CONTROL_PATH: str = "/api/v1/control/from-edge"
    CLOUD_SYNC_OUTBOX_ALARM_PATH: str = "/api/v1/alarms/from-edge"
    CLOUD_SYNC_OUTBOX_OTA_PATH: str = "/api/v1/ota/from-edge"
    CLOUD_SYNC_OUTBOX_PREFLIGHT_PATH: str = "/api/v1/preflight/from-edge"
    CLOUD_SYNC_OUTBOX_CONFIG_PATH: str = "/api/v1/config-versions/from-edge"
    DEVICE_IDENTITY_PATH: str = "/var/lib/drycontroller/device.json"
    CLOUD_IDENTITY_PUBLIC_KEY_FILE: str = "/etc/drycontroller/trust/device-identity-cloud.pub"
    CLOUD_REQUEST_NONCE_DB: str = "/var/lib/drycontroller-cloud-request-nonces/nonces.sqlite3"

    # Append-only operational mirrors. Retention runs once on startup and then daily.
    DATA_RETENTION_ENABLED: bool = True
    DATA_RETENTION_INTERVAL_SEC: float = 24 * 60 * 60
    DATA_RETENTION_BATCH_SIZE: int = 10_000

    # 云端 ops 入口与 Tailnet 身份（由 bootstrap 写入 .env）
    CLOUD_OPS_BASE_URL: str = ""
    TS_LOGIN_SERVER: str = ""
    TAILSCALE_EXPECTED_VERSION: str = ""

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

    def validate_startup_security(self) -> None:
        env = (self.ENV or "").strip().lower()
        if env not in {"prod", "production"}:
            return

        def _is_weak_secret(value: str, default: str) -> bool:
            secret = (value or "").strip()
            return not secret or secret == default or secret.lower().startswith("change_me")

        weak = []
        if _is_weak_secret(self.POSTGRES_PASSWORD, "edge_password"):
            weak.append("POSTGRES_PASSWORD")
        if _is_weak_secret(self.MQTT_PASSWORD, "mqtt_password"):
            weak.append("MQTT_PASSWORD")
        if weak:
            names = ", ".join(weak)
            raise RuntimeError(f"production edge-api startup requires non-default secrets: {names}")

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
                except json.JSONDecodeError:
                    # 回退到逗号分隔
                    pass
            return [item.strip() for item in raw.split(",") if item.strip()]
        return []

    @field_validator("CONTROL_API_KEYS", "ADMIN_API_KEYS", "READONLY_WS_ALLOWLIST", mode="before")
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
                except json.JSONDecodeError:
                    pass
            return [item.strip() for item in raw.split(",") if item.strip()]
        return value or []


# 创建全局配置实例
settings = Settings()
