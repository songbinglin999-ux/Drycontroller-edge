## 共识：需求与验收标准（CONSENSUS）

### 1. 目标复述

- **核心目标**：在开发环境中，通过以下任一方式启动，即可一次性拉起全部服务：
  - `bash scripts/dev-start.sh`
  - `docker-compose up -d`
- **达到状态**：
  - 所有在 `docker-compose.yml` 中定义的服务处于 `Up`（如有 healthcheck 则为 `healthy`）。
  - 核心 HTTP 服务的 `/health` 接口可访问并返回表示 OK/healthy 的 JSON。
  - MQTT、InfluxDB、PostgreSQL 均能通过简单命令行或 Web UI 进行连通性验证。

### 2. 范围内的服务与组件

- **应用服务**
  - `edge-api`：FastAPI 网关，提供 `/health`。
  - `edge-daq`：数据采集守护进程，无 HTTP 接口。
  - `ai-softsensor`：AI 软测量守护进程，无 HTTP 接口。
  - `ai-ctrl-optimizer`：AI 控制优化守护进程，无 HTTP 接口。
  - `license-service`：FastAPI 授权服务，提供 `/health`。
  - `edge-monitor`：系统监控守护进程，无 HTTP 接口。
- **基础设施**
  - `postgres`：PostgreSQL 15，使用 `infra/postgres/init.sql` 初始化。
  - `influxdb`：InfluxDB 2.7，使用 `DOCKER_INFLUXDB_INIT_*` 初始化。
  - `mosquitto`：MQTT Broker，禁用匿名访问，使用密码文件。

### 3. 环境变量与配置约定

- `.env.example` 将作为统一的环境变量模板，满足：
  - 覆盖 `docker-compose.yml` 中引用的所有 `${VAR}`。
  - 与 `README.md` / `QUICKSTART.md` 中示例配置语义一致。
- **关键变量约定（开发默认值）**：
  - PostgreSQL：
    - `POSTGRES_HOST=postgres`
    - `POSTGRES_PORT=5432`
    - `POSTGRES_USER=edge_user`
    - `POSTGRES_PASSWORD=edge_password_2025`
    - `POSTGRES_DB=edge_db`
  - InfluxDB：
    - `INFLUXDB_HOST=influxdb`
    - `INFLUXDB_PORT=8086`
    - `INFLUXDB_USERNAME=admin`
    - `INFLUXDB_PASSWORD=admin_password_2025`
    - `INFLUXDB_ORG=edge-org`
    - `INFLUXDB_BUCKET=industrial-data`
    - `INFLUXDB_TOKEN=edge-influxdb-admin-token`
  - MQTT：
    - `MQTT_BROKER=mosquitto`
    - `MQTT_PORT=1883`
    - `MQTT_USER=edge_mqtt`
    - `MQTT_PASSWORD=mqtt_password_2025`
  - 其他：
    - `EDGE_API_PORT=8000`
    - `LICENSE_SERVICE_PORT=8100`
    - `LOG_LEVEL=INFO`
    - `PLC_HOST=192.168.1.100`（开发可配合 `USE_SIMULATOR=true` 使用模拟器）
    - `PLC_PORT=502`
    - `PLC_SLAVE_ID=1`
    - `PLC_SCAN_INTERVAL=1.0`
    - `USE_SIMULATOR=true`
    - `AI_MODEL_PATH=/app/models/softsensor.onnx`
    - `LICENSE_CHECK_ENABLED=false`
    - `LICENSE_EXPIRY_DAYS=365`

### 4. 验收标准（第一步最终目标）

**通过以下检查即视为通过本阶段验收：**

1. **容器状态**
   - 命令：`docker-compose ps`
   - 条件：所有列出的服务均为 `Up` 状态（如 `edge-api`, `edge-daq`, `ai-softsensor`, `ai-ctrl-optimizer`, `license-service`, `edge-monitor`, `postgres`, `influxdb`, `mosquitto`）。

2. **API 健康检查**
   - 命令：
     - `curl http://localhost:8000/health`（edge-api）
     - `curl http://localhost:8100/health`（license-service）
   - 条件：HTTP 200，JSON 中包含 `status` 或 `success` 且为 OK/healthy。

3. **MQTT 发布/订阅**
   - 示例命令：
     - 订阅：`mosquitto_sub -h localhost -p 1883 -u edge_mqtt -P mqtt_password_2025 -t "test/topic" -v`
     - 发布：`mosquitto_pub -h localhost -p 1883 -u edge_mqtt -P mqtt_password_2025 -t "test/topic" -m "hello-edge"`
   - 条件：订阅端能收到发布的测试消息。

4. **InfluxDB 连通性**
   - Web UI：浏览器访问 `http://localhost:8086`，使用 `.env` 中的用户名/密码登录。
   - API：使用 Token 访问 `GET http://localhost:8086/api/v2/buckets`（可选）。
   - 条件：能成功登录并看到默认 Org 与 Bucket。

5. **PostgreSQL 连通性**
   - 宿主机命令：
     - `docker-compose exec postgres psql -U edge_user -d edge_db -c "\dt"`
   - 条件：能连接成功并列出部分已初始化的表。

### 5. 实施策略与约束

- **优先复用**：保持现有 `docker-compose.yml` 与服务代码结构不变，仅补充运行所必需的配置与文档。
- **最小改动原则**：除非影响启动或连通性，不在本阶段大幅改动业务逻辑。
- **开发友好**：默认配置适合单机开发，后续可在运维文档中补充生产环境重写策略。


