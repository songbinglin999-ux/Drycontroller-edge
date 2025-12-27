# 🔁 自维护与自愈机制规范（Self-Healing）

> **范围**：`Drycontroller-edge` 单台边缘机离线 7x24  
> **对齐原则**：本文档以当前 `edge-monitor` 代码实现为准；规划项会明确标注。  
> **更新时间**：2025-12-26

---

## 1. 目标与原则

- **高可用**：单点故障可自动恢复（以容器重启为主），支持 7x24 长时间运行。
- **可观测**：自愈动作必须可追溯（日志 + 事件）。
- **可控可熔断**：避免“重启风暴”；支持人工禁用/手动触发。
- **责任边界**：`edge-monitor` 负责“发现 + 尝试修复 + 上报”，不替代 PLC/硬件安全联锁。

---

## 2. 自愈对象与级别

### 2.1 服务级（已实现）

- 目标：`edge-api`、`edge-daq`、`ai-softsensor`、`ai-ctrl-optimizer`、`license-service`、`edge-monitor` 等
- 手段：HTTP/TCP/容器探活 → 阈值触发 → Docker 容器重启 → 冷却观察 → 熔断升级

### 2.2 数据级（部分实现/部分规划）

- **已实现**：`edge-monitor` 可对 Influx/MQTT 等做 HTTP/TCP 探测（是否纳入 targets 由 `monitor.yaml` 决定）
- **规划**：Influx 不可用时本地缓存补写；Postgres 不可用时 edge-api 只读降级；PLC 断链标准化上报

### 2.3 AI/OTA 级（规划）

- AI 模型多版本登记/校验/回滚闭环（当前未形成完整实现）
- OTA 蓝绿发布与自动判定回滚（当前以脚本人工回滚为主）

---

## 3. 健康检查与状态契约

### 3.1 `/health` vs `/ready`

- **`/health`（liveness）**：进程是否存活，返回轻量信息
- **`/ready`（readiness）**：依赖是否就绪

当前实现：

- `edge-api` 已实现 `/ready`：并发探测 Postgres/Influx/MQTT/事件总线/WS 模块，不就绪返回 **HTTP 503**。
- `edge-monitor` 探活聚合：对每个 target 的 HTTP/TCP/容器检查结果取“最严重状态”作为 overall status。

> 说明：监控探活建议优先探测 `/ready`（可用性），而不仅是 `/health`（存活）。

---

## 4. 自愈策略（已实现）

### 4.1 关键配置（`configs/monitor.yaml`）

- **`services.interval`**：服务探活间隔（秒）
- **`services.failure_threshold`**：连续失败阈值（达到才会触发重启）
- **`services.recovery_observe`**：重启后的观察轮数（用于冷却时长）
- **`services.max_restart_per_hour`**：每小时最大自动重启次数（达到则熔断升级）
- **target 覆盖**：单个服务可覆盖上述阈值/开关（`targets[].self_heal.*`）

### 4.2 状态机（实现要点）

`edge-monitor` 会维护每个服务的自愈上下文（示例字段）：

- `consecutive_failures`：连续失败次数
- `restart_count_hour` / `restart_window_start`：每小时重启计数窗口
- `cooldown_until`：冷却截止时间（避免连续重启）
- `last_action*`：最近自愈动作与原因

### 4.3 决策动作

- **noop**：阈值未到/处于冷却/自愈禁用
- **restart**：达到阈值且未触发熔断限制 → 重启目标容器
- **escalate**：达到每小时重启上限 → 进入更长冷却并等待人工介入

---

## 5. 自愈执行与审计（已实现）

### 5.1 执行方式

- 通过 Docker API 重启指定容器（依赖 Docker 引擎访问能力，常见为挂载 `/var/run/docker.sock`）。

### 5.2 审计与事件上报

自愈动作会：

- 记录结构化日志（包含 service、action、reason、cooldown 等）
- 通过 MQTT 发布自愈事件（默认主题 `events/system/self_heal`）
- 在系统状态快照里附带 `self_heal.last_outcome`（供 `edge-api`/ops-console 展示）

---

## 6. 运维接口（已实现，但必须做安全加固）

`edge-monitor` 提供运维接口（例如重启、开关自愈、拉取日志、查询告警）。这些接口属于**高危面**：

- 在 7x24 交付形态中，必须做到：**鉴权 + 网络隔离 + 最小暴露面**。
- 推荐：仅允许 `edge-api` 代理访问，宿主机层面默认不开放对外端口；或仅绑定本机回环地址。

---

## 7. 当前缺口与后续规划（面向 7x24 验收）

- **P0（必须）**：`edge-monitor` 的 `/ops/*` 接口鉴权；默认不向宿主机暴露该端口；docker.sock 风险隔离方案落地。
- **P1**：AI 模型版本/校验/回滚闭环；Influx/DB 降级策略与补写机制；运维操作审计入库。
- **P2**：OTA 蓝绿发布与自动回滚；故障注入与演练脚本（断链、磁盘满、MQTT 抖动）。


