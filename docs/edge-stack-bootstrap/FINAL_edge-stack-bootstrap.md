## FINAL 总结 - edge-stack-bootstrap（第一阶段）

### 实现概览

- 复用现有 `docker-compose.yml` 中的服务定义，统一以 `edge-network` 作为容器间通信网络。
- 确认并利用已有的应用实现：
  - `edge-api` 提供 `/health`、`/ready` 接口，并在启动时自动初始化 PostgreSQL 数据库与 MQTT 连接。
  - `license-service` 提供 `/health` 与 `/verify` 接口。
  - `edge-daq`、`ai-softsensor`、`ai-ctrl-optimizer`、`edge-monitor` 作为长期运行的守护进程。
- 在 `docs/edge-stack-bootstrap` 下补充 6A 所需的 ALIGNMENT / CONSENSUS / ACCEPTANCE / TODO / FINAL 文档，明确了需求、约束与验收步骤。
- 追加 `scripts/smoke-test.sh`，可一键串行验证 `/health`、PostgreSQL、InfluxDB 与 MQTT 回环，方便日常自检。

### 关键配置与约定

- 环境变量通过 `.env`（从 `.env.example` 复制或手动创建）注入，覆盖：
  - PostgreSQL：主机、端口、用户名、密码、数据库名。
  - InfluxDB：主机、端口、初始化用户名/密码、Org、Bucket、Admin Token。
  - MQTT：Broker 地址、端口、用户名、密码。
  - 应用端口、日志级别、PLC 及 AI 模型等。
- Mosquitto 禁止匿名访问，需要使用 `mosquitto_passwd` 或脚本生成密码文件来保证 MQTT 发布/订阅可用。

### 验收结论

- 通过 `docker-compose up -d`（在正确准备 `.env` 和 MQTT 密码文件后）可以一次性启动：
  - `edge-api`、`edge-daq`、`ai-softsensor`、`ai-ctrl-optimizer`、`license-service`、`edge-monitor`。
  - `postgres`、`influxdb`、`mosquitto`。
- 通过文档中列出的命令，可以验证：
  - `edge-api` 与 `license-service` 的 `/health` 返回正常。
  - MQTT 可完成基本的发布/订阅测试。
  - InfluxDB 与 PostgreSQL 均可成功连接并访问基础资源。

本阶段聚焦于“所有容器可启动 + 基础连通性验证”，后续高级功能与生产级强化见 `TODO_edge-stack-bootstrap.md`。


