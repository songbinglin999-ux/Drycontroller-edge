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

# 兼容新版本 mosquitto：/mosquitto/config 通常为只读挂载(root:root)，ACL 文件会被拒绝加载。
# 我们把 ACL 复制到可写的 /mosquitto/data，并设置安全权限与属主（uid=1883）。
ACL_SRC="${CFG_DIR}/acl"
ACL_DST="/mosquitto/data/acl"
if [ -f "$ACL_SRC" ]; then
  mkdir -p /mosquitto/data
  cp -f "$ACL_SRC" "$ACL_DST"
  chmod 600 "$ACL_DST" || true
  chown 1883:1883 "$ACL_DST" || true
fi

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


