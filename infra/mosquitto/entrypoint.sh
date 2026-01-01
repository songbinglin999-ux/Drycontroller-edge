#!/bin/sh
set -eu

# 运行时选择配置：
# - MQTT_TLS=true  -> mosquitto.tls.conf（仅 TLS 端口）
# - 否则          -> mosquitto.conf（明文端口，仅用于本机/开发；生产建议使用 TLS）

TLS="${MQTT_TLS:-false}"
TLS="$(printf '%s' "$TLS" | tr '[:upper:]' '[:lower:]')"

CFG_DIR="/mosquitto/config"
PLAIN="${CFG_DIR}/mosquitto.conf"
TLS_CFG="${CFG_DIR}/mosquitto.tls.conf"

if [ "$TLS" = "true" ] || [ "$TLS" = "1" ] || [ "$TLS" = "yes" ]; then
  if [ ! -f "$TLS_CFG" ]; then
    echo "[mosquitto] missing $TLS_CFG" >&2
    exit 2
  fi
  echo "[mosquitto] TLS enabled, using mosquitto.tls.conf"
  exec /usr/sbin/mosquitto -c "$TLS_CFG"
else
  if [ ! -f "$PLAIN" ]; then
    echo "[mosquitto] missing $PLAIN" >&2
    exit 2
  fi
  echo "[mosquitto] TLS disabled, using mosquitto.conf"
  exec /usr/sbin/mosquitto -c "$PLAIN"
fi


