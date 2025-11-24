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
  - 可选：写入 `/logs/{service}/app.log`
- 宿主机：
  - 通过 Docker log driver 或 sidecar 将日志汇聚到统一目录

---

## 3. 日志格式

- 推荐使用 **JSON Lines** 格式：每行一个 JSON 对象  
- 基本字段：
  - `timestamp`：时间（ISO8601）  
  - `level`：`DEBUG` / `INFO` / `WARN` / `ERROR`  
  - `service`：服务名（如 `edge-api`、`edge-daq`）  
  - `module`：代码模块（可选）  
  - `message`：简要说明  
  - `trace_id` / `request_id`：链路标识（如有）  
  - `extra`：扩展字段（字典）

示例：

```json
{
  "timestamp": "2025-01-01T12:00:00Z",
  "level": "INFO",
  "service": "edge-daq",
  "module": "plc_reader",
  "message": "Read tags from PLC",
  "extra": {
    "tags_count": 128,
    "duration_ms": 35
  }
}
```

---

## 4. 各服务关键日志点

### 4.1 edge-daq

- PLC 连接成功/失败  
- 采集循环耗时异常（超出阈值）  
- Tag 解析错误 / 值越界（与 `limits.yaml` 比较）

### 4.2 ai-softsensor

- 模型加载/切换/回滚  
- 推理延迟统计  
- 结果异常（含水率超范围、偏差过大）

### 4.3 edge-api

- API 请求访问日志（方法、路径、状态码、耗时）  
- 控制指令下发与结果  
- 配置变更操作（谁、改了什么）

### 4.4 edge-monitor

- 自愈动作记录（哪个服务、重启/熔断原因）  
- 资源使用超阈值事件（CPU/RAM/Disk）

---

## 5. 日志级别使用约定

- `DEBUG`：调试信息，默认生产环境可关闭  
- `INFO`：正常流程关键节点，如服务启动、定期统计  
- `WARN`：可疑或轻微异常，但系统仍可运行  
- `ERROR`：业务失败或功能受损，需要人工关注  
- `FATAL`：系统无法继续运行，需要立即重启或停机

---

## 6. 日志轮转与保留策略

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