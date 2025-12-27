# 📜 日志规范与可观测性（Logging Standard）

> **范围**：`Drycontroller-edge`（离线 7x24）  
> **对齐原则**：本文档以 `common/logging` 与各服务当前落地方式为准。  
> **更新时间**：2025-12-26

---

## 1. 目标

- **统一结构化日志**：便于现场排障、长期留存与后续接入 ELK/Loki。
- **统一事件审计**：关键动作（启动/停止/异常/自愈）可追溯（日志 + 事件）。
- **可观测三件套**：Logs（日志）+ Metrics（指标）+ Events（事件）。

---

## 2. 日志输出位置（统一落盘 + stdout）

- **容器内**
  - stdout：便于 `docker logs`
  - 文件：`/logs/<service>/app.log`（JSON Lines）
- **宿主机**
  - `./logs/<service>/` 通过 volume 映射到容器内 `/logs/<service>/`

> 生产建议：对 `./logs` 配置 logrotate（见 `docs/ops/deployment-guide.md`）。

---

## 3. 日志格式（JSON Lines + structlog）

### 3.1 基本约定

- 每行一个 JSON 对象（JSONL）
- 统一由 `structlog` 序列化输出，避免手工 `json.dumps`

### 3.2 推荐字段

- `timestamp`：ISO8601（UTC）
- `level`：info/warn/error…
- `service`：服务名（建议在 logging bootstrap 中注入）
- `module`：模块名
- `message`：事件名（用“事件名 + 上下文字段”的方式写）
- `request_id`：HTTP/WS 请求链路标识（`edge-api` 已在 middleware 注入 `X-Request-ID`）
- 业务上下文字段：如 `tag`、`line`、`model_version`、`container`、`action`、`reason`

示例：

```json
{"timestamp":"2025-12-26T12:00:00Z","level":"info","service":"edge-api","module":"app.main","message":"request_completed","request_id":"...","path":"/ready","status_code":200,"process_time":"0.012s"}
```

---

## 4. 事件（Events）写入与发布

### 4.1 事件记录器

项目内使用 `common.logging.setup_logging(...)` 初始化日志，并可获得事件记录器（用于记录关键事件）。

建议所有服务至少记录：

- `service_starting` / `service_started`
- `service_stopping` / `service_stopped`
- `service_error`（包含 error/stack）
- `unhandled_exception`（全局兜底）

### 4.2 自愈事件（edge-monitor）

`edge-monitor` 在执行自动重启/熔断等动作时，会输出结构化日志并发布自愈事件（默认 MQTT 主题 `events/system/self_heal`），用于 ops-console/大屏侧展示与追溯。

---

## 5. 指标（Metrics）

- `edge-api`：提供 `/metrics`（Prometheus exposition）
- `prometheus`：采集指标并触发告警规则
- `alertmanager`：聚合告警并提供查询接口（ops-console 可读）

---

## 6. 现场排障建议（7x24 视角）

- 优先看系统状态：`edge-api /api/v1/system/status`（来自 `edge-monitor` 发布）
- 探活判定以 `/ready` 为准（503 = 不可用/依赖未就绪）
- 自愈风暴排查：订阅 `events/system/self_heal`，观察是否频繁 `restart_limit_reached`


