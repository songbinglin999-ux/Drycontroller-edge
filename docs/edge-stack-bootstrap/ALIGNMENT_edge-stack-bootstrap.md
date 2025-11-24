## 背景与目标（Align）

- **项目背景**：工业烘干边缘控制系统，已有完整的多服务代码结构与 `docker-compose.yml`，但需要保证在任何干净环境下，开发者只需配置环境变量并执行一次 `docker-compose up -d` 即可拉起全部服务。
- **本次任务名**：`edge-stack-bootstrap`
- **当前状态**：
  - 已存在的服务与目录：
    - 应用服务：`edge-api`、`edge-daq`、`ai-softsensor`、`ai-ctrl-optimizer`、`license-service`、`edge-monitor`
    - 基础设施：`postgres`、`influxdb`、`mosquitto`
    - Compose 与脚本：`docker-compose.yml`、`scripts/dev-start.sh`
  - 主要应用服务均已实现核心逻辑，并非空壳；`edge-api` 与 `license-service` 已有 `/health` 接口。

## 需求理解

- **一键启动要求**
  - 执行 `docker-compose up -d` 能够：
    - 拉起所有定义的容器（包括应用与基础设施）。
    - 即便业务逻辑未完全实现，容器也不会因缺配置或依赖而直接崩溃。
- **连通性与功能性要求（第一步最终目标）**
  - 所有容器处于 `Up` 状态，必要时带 `healthy`。
  - 所有容器位于同一 Docker 网络内，服务之间可以通过服务名互相访问（如 `postgres`, `influxdb`, `mosquitto` 等）。
  - HTTP API：
    - 至少 `edge-api` 与 `license-service` 提供 `/health` 接口，并在正常情况下返回 OK/healthy 状态。
  - MQTT：
    - Mosquitto Broker 可用，支持认证登录。
    - 能通过外部客户端或系统内部组件实现发布/订阅（哪怕是假数据）。
  - InfluxDB：
    - InfluxDB 2.x 能完成初始化（用户名、密码、Org、Bucket、Token）。
    - 可通过 URL `http://influxdb:8086` 使用初始化的 Token 连接。
  - PostgreSQL：
    - Postgres 容器完成初始化脚本执行。
    - 能从其它容器或宿主机用 `POSTGRES_*` 环境变量连接。

## 边界与非目标

- 本阶段 **不要求**：
  - 实现完整的业务 API、AI 推理与控制策略（允许保留 `TODO` 或模拟实现）。
  - 配置生产级监控、告警与高可用（当前仅需单节点开发环境）。
  - 完整的安全加固（密码/Token 以开发环境为主，后续可再强化）。
- 本阶段 **只保证**：
  - 开发者在本机按文档操作即可顺利启动与基本验证连通性。

## 现有约束与决策依据

- **技术栈**：Python 3.11、FastAPI、AsyncIO、PostgreSQL、InfluxDB 2.x、Mosquitto、Docker Compose。
- **配置加载方式**：
  - 应用服务主要通过 `.env`（Pydantic Settings）+ Docker Compose 环境变量注入。
  - `scripts/dev-start.sh` 约定从 `.env.example` 复制生成 `.env`。
- **MQTT 安全策略**：
  - Mosquitto 禁止匿名登录，必须有密码文件。
  - 脚本中约定默认用户为 `edge_mqtt`，密码为 `mqtt_password_2025`。
- **时序库初始化**：
  - `docker-compose.yml` 使用 `DOCKER_INFLUXDB_INIT_*` 环境变量执行一次性初始化。
- **业务库初始化**：
  - `infra/postgres/init.sql` 包含较完整的 schema 与初始数据。

## 待确认/假设（无用户进一步说明前默认成立）

1. **假设**：当前仓库主要用于开发/演示，允许在 `.env.example` 中提供开发默认密码与 Token（不会直接用于生产）。
2. **假设**：`docker-compose` 使用的环境变量集合以当前 `docker-compose.yml` 与 `README.md` 为准，无需引入额外变量名称。
3. **假设**：在本阶段只需要人工用 CLI / 浏览器验证 MQTT、InfluxDB、PostgreSQL 的连通性，而不要求自动化测试脚本。
4. **假设**：`ai-softsensor`、`edge-daq` 等服务即便无法真正访问现场 PLC 或真实模型，只要不异常退出即可视为“启动成功”。

如以上假设有偏差，请在后续阶段指出，我会在共识文档中调整。


