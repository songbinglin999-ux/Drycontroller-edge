# 🔄 数据流与消息流架构（Data Flow）

> 版本：v1.0  
> 关联文档：`system-overview.md` 第 5 章、`PROJECT_STRUCTURE.md`

---

## 1. 总览

本系统数据流可以拆分为四条主链路：

1. **采集链路**：PLC → `edge-daq` → MQTT → InfluxDB  
2. **AI 链路**：MQTT → `ai-softsensor` → MQTT / InfluxDB → `ai-ctrl-optimizer`  
3. **控制链路**：`ai-ctrl-optimizer` → `edge-api` → （大屏 / PLC）  
4. **配置与管理链路**：`edge-api` ↔ PostgreSQL ↔ 配置文件（YAML）

所有链路通过 **MQTT + REST API** 解耦，支持模块独立升级与伸缩。

---

## 2. 采集链路：PLC → edge-daq → MQTT → InfluxDB

### 2.1 点位分类

主要采集点位包括（示例）：

- 一段烘干区温度  
- 二段烘干区温度  
- 尾气温度（预烘干热源）  
- 预烘干出口料温  
- 料斗绞龙扭矩  
- 链速  
- 风机频率  
- 炉温  
- 成品人工含水率（人工录入）

具体点位定义与命名规则参见：`docs/plc/tag-definition-standard.md`。

### 2.2 数据流描述

```text
PLC（Modbus TCP）
   ↓ 轮询读取
edge-daq（采集服务）
   ↓ MQTT 发布（原始/预处理数据）
MQTT Broker（Mosquitto）
   ↓ 订阅
ai-softsensor / ai-ctrl-optimizer / edge-api
   ↓ 写入
InfluxDB（时序数据库）
```

### 2.3 采集频率与 QoS

- 默认采集频率：**1 Hz**（可配置）  
- MQTT QoS 建议：
  - 关键工艺点：QoS 1（至少一次，避免丢失）  
  - 普通监控点：QoS 0

---

## 3. AI 链路：软测量与控制优化

### 3.1 软测量数据流

```text
edge-daq → MQTT（topic: process/raw/#）
   ↓ 订阅
ai-softsensor（特征预处理 + ONNX 推理）
   ↓
   ├→ MQTT（topic: ai/moisture/estimation）
   └→ InfluxDB（measurement: ai_moisture）
```

流程步骤：

1. `ai-softsensor` 订阅原始工艺数据（温度、链速、扭矩等）  
2. 进行特征工程（滑动窗口、归一化、异常值过滤）  
3. 调用 ONNX 模型进行含水率预测  
4. 将预测结果写入 MQTT 与 InfluxDB  
5. 其它服务（如 `ai-ctrl-optimizer`、`edge-api`）通过订阅或查询获取结果

### 3.2 控制优化数据流

```text
InfluxDB / MQTT → ai-ctrl-optimizer
   ↓ 计算最优 setpoint
MQTT（topic: control/suggestion/#）
   ↓
edge-api / 大屏 / PLC（未来闭环）
```

说明：

- 当前阶段主要输出 **控制建议**（链速、抽风频率等），由人工确认后下发  
- 未来可扩展为 **全自动闭环控制**：`ai-ctrl-optimizer → MQTT → edge-daq → PLC`

---

## 4. 控制链路：建议、确认与下发

### 4.1 控制建议生成

- 输入：
  - 实时工艺数据（温度、链速等）
  - AI 含水率估计值
  - 工艺限制 / 安全约束（来自 `configs/limits.yaml`）
- 输出：
  - 建议链速（m/min）
  - 建议抽风频率（Hz）
  - 建议执行时机（立即 / 延迟）

### 4.2 人工确认与下发

控制流程示意：

```text
ai-ctrl-optimizer → MQTT（control/suggestion/#）
   ↓ 订阅
edge-api → WebSocket / REST → 大屏
   ↓ 人工确认
（未来）→ MQTT（control/apply/#）→ edge-daq → PLC
```

当前阶段建议：

- 所有控制信号经过 **人工确认** 后再下发至 PLC  
- 所有控制操作必须记录在 PostgreSQL 的“控制操作日志”表中

---

## 4.4 WebSocket 推流总线

```
edge-daq / AI / edge-monitor → MQTT (realtime/#, ai/#, system/#)
                                        │
                                        ▼
                               edge-api WS Relay
                                        │
               ┌────────────────────────┼────────────────────────┐
               │                        │                        │
         /api/ws/realtime         /api/ws/ai              /api/ws/system
```

- WebSocket 推送与 MQTT 保持相同消息结构 `{line, tag, value, ts}`；
- 支持按 `line`、`tags` 过滤，默认订阅所有产线；
- 最新系统健康可以通过 REST `/api/system/health` 拉取，也可以订阅 `/api/ws/system` 获取实时状态；
- 典型订阅示例：

```
websocat ws://edge-api:8000/api/ws/realtime?line=L1&tags=DRYER1.IN.TEMP
websocat ws://edge-api:8000/api/ws/ai?line=L1
websocat ws://edge-api:8000/api/ws/system
```

大屏或第三方应用只需依赖 edge-api，即可同时获取 HTTP 历史数据与 WS 实时数据。

---

## 5. 配置与管理链路

### 5.1 配置来源

配置分为两类：

- **静态配置（文件）**：存放于 `configs/` 目录
  - `tags.yaml`：PLC 标签与 MQTT topic 映射  
  - `ai-config.yaml`：模型版本、特征配置  
  - `limits.yaml`：工艺上下限、报警阈值  
  - `qos.yaml`：采集频率、MQTT QoS 等
- **动态配置（数据库）**：存放于 PostgreSQL
  - 用户与权限  
  - 报警规则  
  - 控制策略参数  
  - 配置变更记录

### 5.2 配置流转

```text
配置文件（YAML） ↔ 初始化 / 运维工具 ↔ PostgreSQL
                              ↓
                         edge-api
                              ↓
                        其它服务（订阅配置）
```

要求：

- 通过统一 API（`edge-api`）修改配置时必须记录审计日志  
- 服务支持在不重启的情况下 **热加载关键配置**（例如采集频率、报警阈值）

---

## 6. 事件与日志数据流

### 6.1 事件流

事件类型包括：

- PLC 断连 / 重连  
- AI 降级 / 模型回滚  
- OTA 升级 / 回滚  
- 服务自愈动作（重启、熔断）

事件流：

```text
各服务 → MQTT（events/#） → edge-monitor / edge-api → 大屏 / 报警系统
```

### 6.2 日志流

- 建议所有服务输出 **结构化 JSON 日志** 至本地文件：`/logs/{service}/app.log`  
- 可选：通过 Filebeat / Fluentd 推送到集中日志系统（如 ELK / Loki）

详细规范见：`docs/ops/logging-standard.md`。

---

## 7. 数据一致性与可靠性

### 7.1 时序数据

- 以 **最终一致性** 为主：短时网络抖动允许少量数据延迟写入  
- 对关键指标（如 AI 结果）建议启用批量写入与重试机制

### 7.2 业务数据

- 配置与控制操作记录必须保证 **事务一致性**（PostgreSQL 事务）  
- 跨服务操作需通过 `edge-api` 聚合，禁止在多个服务中直接写库

---

## 8. 面向扩展的数据流设计

系统未来扩展场景：

- 多产线 / 多工控机协同  
- 增加新的 AI 模型（如设备故障预测）  
- 引入云端管理平台

数据流扩展原则：

- 所有新模块均应 **通过 MQTT 与 REST API 接入**，避免点对点耦合  
- 统一使用命名规范良好的 MQTT Topic 与 Measurement 名称  
- 避免在 AI 服务中直接访问 PLC / 数据库，统一走消息总线与 API


