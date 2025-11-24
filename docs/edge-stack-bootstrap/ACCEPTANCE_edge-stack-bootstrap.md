## edge-stack-bootstrap 验收记录（第一阶段）

> 目标：`docker-compose up -d` 能一次性启动全部服务，并完成基础连通性验证。

### 1. 环境准备

1. 复制环境变量：
   - 若存在：`cp .env.example .env`
   - 若仓库中没有 `.env.example` 文件，请参考下方示例自行创建：

   ```bash
   # 数据库
   POSTGRES_HOST=postgres
   POSTGRES_PORT=5432
   POSTGRES_USER=edge_user
   POSTGRES_PASSWORD=edge_password_2025
   POSTGRES_DB=edge_db

   # InfluxDB
   INFLUXDB_HOST=influxdb
   INFLUXDB_PORT=8086
   INFLUXDB_USERNAME=admin
   INFLUXDB_PASSWORD=admin_password_2025
   INFLUXDB_ORG=edge-org
   INFLUXDB_BUCKET=industrial-data
   INFLUXDB_TOKEN=edge-influxdb-admin-token

   # MQTT
   MQTT_BROKER=mosquitto
   MQTT_PORT=1883
   MQTT_USER=edge_mqtt
   MQTT_PASSWORD=mqtt_password_2025

   # 端口与其他
   EDGE_API_PORT=8000
   LICENSE_SERVICE_PORT=8100
   LOG_LEVEL=INFO
   PLC_HOST=192.168.1.100
   PLC_PORT=502
   PLC_SLAVE_ID=1
   PLC_SCAN_INTERVAL=1.0
   USE_SIMULATOR=true
   AI_MODEL_PATH=/app/models/softsensor.onnx
   LICENSE_CHECK_ENABLED=false
   LICENSE_EXPIRY_DAYS=365
   ```

2. （推荐）创建 Mosquitto 密码文件（若未使用 `scripts/dev-start.sh`）：

   ```bash
   # 需已安装 mosquitto_passwd
   mosquitto_passwd -b infra/mosquitto/passwords edge_mqtt mqtt_password_2025
   ```

### 2. 启动容器

```bash
docker-compose up -d
```

预期：

- `docker-compose ps` 中所有服务状态为 `Up`（postgres/influxdb/mosquitto 可能带有 `healthy` 标记）。

### 3.（可选）一键烟雾测试

执行脚本即可自动串行验证 `/health`、PostgreSQL、InfluxDB 与 MQTT 流程：

```bash
bash scripts/smoke-test.sh
```

脚本依赖：

- `.env` 已配置完整变量；
- 已经执行 `docker-compose up -d` 并成功启动全部容器；
- `docker-compose`、`curl` 可在宿主机上使用。

若脚本结尾输出 `🎉 烟雾测试全部通过！`，说明本阶段的“第一步最终目标”已满足，无需手工逐项验证。若脚本中断，可参考后续步骤手动排查。

### 4. API 健康检查

```bash
curl http://localhost:8000/health
curl http://localhost:8100/health
```

预期：

- `edge-api` 返回 JSON 包含 `success: true`、`status: "healthy"`。
- `license-service` 返回 `{"status": "healthy"}`。

### 5. MQTT 发布/订阅

1. 打开一个终端订阅：

   ```bash
   mosquitto_sub -h localhost -p 1883 -u edge_mqtt -P mqtt_password_2025 -t "test/topic" -v
   ```

2. 另一个终端发布：

   ```bash
   mosquitto_pub -h localhost -p 1883 -u edge_mqtt -P mqtt_password_2025 -t "test/topic" -m "hello-edge"
   ```

预期：订阅端终端能看到 `test/topic hello-edge`。

### 6. InfluxDB 连通性

- 浏览器访问 `http://localhost:8086`。
- 使用 `.env` 中配置的用户名/密码登录（如：`admin` / `admin_password_2025`）。

预期：

- 能成功登录，并能看到组织 `edge-org` 与 bucket `industrial-data`。

### 7. PostgreSQL 连通性

```bash
docker-compose exec postgres psql -U edge_user -d edge_db -c "\dt"
```

预期：

- 能正常连接并列出一批已初始化的业务表（如 `users`、`licenses` 等）。

### 8. 结论

- 若以上步骤全部通过，则 **edge-stack-bootstrap 第一阶段目标（一次性启动与基础连通性）验收通过**。


