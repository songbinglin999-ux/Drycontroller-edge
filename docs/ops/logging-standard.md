# 📜 日志规范与可观测性（Logging Standard）

> 版本：v1.0  
> 关联文档：`system-overview.md` 第 11 章

---

## 1. 目标

- 所有服务输出统一格式的结构化日志，便于检索与分析  
- 日志中包含关键上下文（服务名、请求 ID、Tag 名、模型版本等）  
- 支持后续对接集中日志系统（ELK / Loki / 其它）

---

## 2. 日志输出位置

- 容器内：
  - 标准输出（stdout）——便于 `docker logs` 查看  
  - **必选**：结构化日志文件 ` /logs/{service}/app.log`
- 宿主机：
  - `./logs/{service}` 通过 volume 映射到容器的 `/logs/{service}`，所有日志统一收集

---

## 3. 日志格式

- 统一使用 **JSON Lines**：每行一个 JSON 对象  
- 必填字段：
  - `timestamp`：ISO8601 时间戳（UTC）  
  - `level`：日志级别  
  - `service`：服务名（自动注入）  
  - `module`：发出日志的 Python 模块  
  - `message`：事件名称（`logger.info("event_name", **context)`）  
  - `env` / `instance`：环境与实例 ID  
  - `extra`：扩展字段（自动来自 `**context`）
- 通过 `common.logging.setup_logging("<service-name>")` 自动配置：

```python
from common.logging import setup_logging
import structlog

event_recorder = setup_logging("edge-api")
logger = structlog.get_logger(__name__)

logger.info("request_completed", request_id="abc", status=200)
event_recorder.record_event("service_started")
```

- JSON 由 `structlog` 统一序列化，禁止手工 `json.dumps`。

示例输出：

```json
{"timestamp":"2025-01-01T12:00:00Z","level":"info","service":"edge-daq","module":"daq.plc_reader","message":"read_tags","tags_count":128,"duration_ms":35}
```

> 注：`common.logging.setup_logging` 会同时写入 stdout 与 `/logs/{service}/app.log`，并注入 `service`、`env`、`instance` 等公共字段。
  
## 4. 关键事件写入 InfluxDB

- `setup_logging` 会返回 `EventRecorder`，并自动使用 `INFLUXDB_*` 环境变量建立连接。
- 使用 `event_recorder.record_event("service_started", severity="info", fields={"version": "1.0.0"})` 将关键事件写入 measurement `service_events`。
- 事件数据结构：
  - Tag：`service`、`event`、`severity`、自定义 `tags`
  - Field：`value`（默认 1）及自定义 `fields`
- 所有服务需至少记录以下事件：
  - `service_starting` / `service_started`
  - `service_stopping` / `service_stopped`
  - `service_error`（含 error 信息）
- Edge Monitor 额外记录 `system_metrics_write_failed` / `service_health_write_failed` 等异常，以支撑自愈与告警。

---

## 5. 各服务关键日志点

### 5.1 edge-daq

- PLC 连接成功/失败  
- 采集循环耗时异常（超出阈值）  
- Tag 解析错误 / 值越界（与 `limits.yaml` 比较）

### 5.2 ai-softsensor

- 模型加载/切换/回滚  
- 推理延迟统计  
- 结果异常（含水率超范围、偏差过大）

### 5.3 edge-api

- API 请求访问日志（方法、路径、状态码、耗时）  
- 控制指令下发与结果  
- 配置变更操作（谁、改了什么）

### 5.4 edge-monitor

- 自愈动作记录（哪个服务、重启/熔断原因）  
- 资源使用超阈值事件（CPU/RAM/Disk）

---

## 6. 日志级别使用约定

- `DEBUG`：调试信息，默认生产环境可关闭  
- `INFO`：正常流程关键节点，如服务启动、定期统计  
- `WARN`：可疑或轻微异常，但系统仍可运行  
- `ERROR`：业务失败或功能受损，需要人工关注  
- `FATAL`：系统无法继续运行，需要立即重启或停机

---

## 7. 日志轮转与保留策略

- 推荐策略（见 `deployment-guide.md` 中 logrotate 示例）：
  - 每日切分  
  - 保留 7–30 天（根据现场磁盘空间）  
  - 旧日志压缩（gzip）  
- 对于审计类日志（控制操作、配置变更）可在数据库中长期保留。

{
  "cells": [],
  "metadata": {
    "language_info": {
      "name": "python"
    }
  },
  "nbformat": 4,
  "nbformat_minor": 2
}