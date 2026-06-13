#!/bin/bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

log_step() {
  printf '\n== %s ==\n' "$1"
}

log_ok() {
  printf '[OK] %s\n' "$1"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "缺少命令：$1，请先安装后再运行脚本。" >&2
    exit 1
  fi
}

if [ ! -f .env ]; then
  echo ".env 文件不存在，请先根据仓库说明创建后再运行本脚本。" >&2
  exit 1
fi

require_cmd docker-compose
require_cmd curl

if ! docker-compose ps >/dev/null 2>&1; then
  echo "docker-compose 无法连接到当前编排，请检查 Docker 服务或是否在正确目录执行。" >&2
  exit 1
fi

# 加载环境变量
set -a
. ./.env
set +a

EDGE_API_PORT="${EDGE_API_PORT:-8000}"
LICENSE_SERVICE_PORT="${LICENSE_SERVICE_PORT:-8100}"
INFLUXDB_PORT="${INFLUXDB_PORT:-8086}"

required_env_vars=(
  POSTGRES_USER
  POSTGRES_DB
  INFLUXDB_TOKEN
  INFLUXDB_ORG
  MQTT_USER_EDGE_API
  MQTT_PASSWORD_EDGE_API
)

for var in "${required_env_vars[@]}"; do
  if [ -z "${!var:-}" ]; then
    echo "环境变量 $var 未设置，请检查 .env。" >&2
    exit 1
  fi
done

log_step "当前容器状态"
docker-compose ps

check_http() {
  local name="$1"
  local url="$2"
  log_step "HTTP 健康检查：$name ($url)"
  local body
  body="$(curl -fsS "$url")"
  echo "响应：$(echo "$body" | tr -d '\n' | cut -c1-160)"
  log_ok "$name /health 正常"
}

check_postgres() {
  log_step "PostgreSQL 连通性"
  docker-compose exec -T postgres psql \
    -U "$POSTGRES_USER" \
    -d "$POSTGRES_DB" \
    -c "SELECT NOW();" >/dev/null
  log_ok "PostgreSQL 连接成功"
}

check_influx() {
  log_step "InfluxDB 连通性"
  curl -fsS \
    -H "Authorization: Token ${INFLUXDB_TOKEN}" \
    "http://localhost:${INFLUXDB_PORT}/api/v2/buckets?org=${INFLUXDB_ORG}" >/dev/null
  log_ok "InfluxDB API 响应正常"
}

check_mqtt() {
  log_step "MQTT 发布/订阅回环"
  local topic="edge/smoke/$RANDOM"
  local payload="edge-smoke-$(date +%s)"
  docker-compose exec -T \
    -e MQTT_USER="$MQTT_USER_EDGE_API" \
    -e MQTT_PASSWORD="$MQTT_PASSWORD_EDGE_API" \
    -e TEST_TOPIC="$topic" \
    -e TEST_PAYLOAD="$payload" \
    mosquitto /bin/sh -c '
      set -e
      TMP_FILE="/tmp/mqtt-smoke-$$"
      mosquitto_sub -h localhost -p 1883 \
        -u "$MQTT_USER" -P "$MQTT_PASSWORD" \
        -t "$TEST_TOPIC" -C 1 >"$TMP_FILE" &
      SUB_PID=$!
      sleep 1
      mosquitto_pub -h localhost -p 1883 \
        -u "$MQTT_USER" -P "$MQTT_PASSWORD" \
        -t "$TEST_TOPIC" -m "$TEST_PAYLOAD"
      wait "$SUB_PID"
      cat "$TMP_FILE"
      rm -f "$TMP_FILE"
    ' >/dev/null
  log_ok "MQTT 发布/订阅成功 (topic: $topic)"
}

check_http "edge-api" "http://localhost:${EDGE_API_PORT}/health"
check_http "license-service" "http://localhost:${LICENSE_SERVICE_PORT}/health"
check_postgres
check_influx
check_mqtt

check_latest_realtime() {
  log_step "Edge API 最新数据"
  local body
  body="$(curl -fsS "http://localhost:${EDGE_API_PORT}/api/realtime/latest?tags=FTT1&lookback=2m")"
  echo "响应：$(echo "$body" | tr -d '\n' | cut -c1-160)"
  log_ok "/api/realtime/latest 正常"
}

check_system_health_ws_cache() {
  log_step "Edge API 系统健康缓存"
  local body
  body="$(curl -fsS "http://localhost:${EDGE_API_PORT}/api/system/health")"
  echo "响应：$(echo "$body" | tr -d '\n' | cut -c1-160)"
  log_ok "/api/system/health 正常"
}

check_latest_realtime
check_system_health_ws_cache

printf '\n🎉 烟雾测试全部通过！\n'

