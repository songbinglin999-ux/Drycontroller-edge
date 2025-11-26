# 🔁 自维护与自愈机制规范（Self-Healing）

> 版本：v1.0  
> 关联文档：`system-overview.md` 第 6 章、自监控服务 `edge-monitor`
>
> ⚠️ **当前状态说明**  
> - `edge-monitor` 已落地系统/服务健康检测、指标写入和 MQTT/Inﬂux 上报。  
> - 自动自愈动作（容器重启、AI 降级、OTA 回滚等）尚未实现，仍处设计阶段。  
> - 下文各章节区分 “当前实现” 与 “计划完善”，避免误判实际能力。

---

## 1. 设计目标

- **高可用**：单点故障不导致系统停摆，支持 24/7 长时间运行  
- **可观测**：所有自愈动作都有事件日志与指标记录，可追溯  
- **可控可熔断**：自愈有边界，避免“疯狂重启”；支持人工一键禁用  
- **可配置**：策略、阈值全部外置配置（YAML / 数据库），不写死在代码里

---

## 2. 自愈对象与责任边界

- **服务级别**
  - `edge-api`、`edge-daq`、`ai-softsensor`、`ai-ctrl-optimizer`、`license-service`、`edge-monitor`
  - Docker 容器健康状态、/health HTTP 接口、进程状态
- **数据级别**
  - PLC 连接状态、MQTT 连接状态、InfluxDB / PostgreSQL 读写状态
- **AI 级别**
  - 模型加载、推理结果合法性、模型版本切换
- **OTA 级别**
  - 升级任务、镜像拉取、容器切换与回滚

边界原则：

- **edge-monitor 只负责“发现 + 尝试修复 + 上报”**  
- **最终是否继续运行/停机由运维人员和生产工艺负责人共同决策**

---

## 3. 服务自愈机制

### 3.1 健康检查规范

- **当前实现**
  - 各服务已提供 `/health`，返回 `success/status/service/version` 等轻量信息。
  - `/ready` 仍保留 TODO，尚未真实检测数据库、MQTT、InfluxDB 等依赖。
- **计划完善**
  - **统一响应契约**：`{"success":true,"status":"ok|degraded|fail","service":"edge-api","uptime_seconds":12345,"dependencies":{"postgres":"ok","mqtt":"fail","influxdb":"ok"}}`；所有服务在 1.2 版本前完成改造。
  - **依赖检测器**：在每个服务内部增加轻量探测（DB ping、MQTT 检查、Influx write/read），并将结果写入 `dependencies` 字段及 Prometheus/MQTT。
  - **readiness 联动**：`/ready` 依赖上述探测，当任一关键依赖为 `fail` 时直接返回 503，并通过 MQTT 通知 `edge-monitor`。

### 3.2 周期性检查

- **当前实现**：`configs/monitor.yaml` 中配置 `system.interval=30s`、`services.interval=20s`，仅执行 HTTP/TCP/Docker 状态采集。
- **计划完善**：
  - `configs/monitor.yaml` 扩展：
    ```yaml
    services:
      interval: 20
      failure_threshold: 3   # N
      recovery_observe: 6    # M
      max_restart_per_hour: 5
      targets:
        - name: edge-api
          url: ...
          self_heal:
            enabled: true
            failure_threshold: 2   # 支持覆盖
            max_restart_per_hour: 3
    ```
  - `edge-monitor` 内部维护每个 target 的连续失败计数、最后恢复时间，供策略模块使用。

### 3.3 重启策略

- **当前实现**：尚未在 `edge-monitor` 中实现自动重启，只做检测与上报。
- **计划完善**：
  1. **检测层**：在 `_service_health_loop` 中调用 `SelfHealDetector`，输出 `status/fail_count/last_success`。
  2. **策略层**：`SelfHealPolicy` 根据配置计算动作为 `noop | restart | escalate`，并生成原因、冷却时间。
  3. **执行层**：`SelfHealExecutor` 通过 Docker SDK 重启指定容器，之后锁定该服务 M 个周期进行观察。
  4. **熔断层**：记录每小时重启次数，超过 `max_restart_per_hour` 将动作降级为 `escalate`，并推送高等级告警至 MQTT `events/system/self_heal`.
  5. **审计**：所有动作写入 structlog、Influx measurement `self_heal_events` 与 PostgreSQL `self_heal_history`（计划新增表）。

---

## 4. 数据自愈机制

### 4.1 PLC 连接

- **当前实现**：`edge-daq` 只在日志中记录读写错误，无统一退避策略，也没有向 `edge-monitor` 上报 “PLC 断连”。
- **计划完善**：
  - `edge-daq` 在 `ModbusClient` 中实现指数退避，最大等待 60s，并将每次失败通过 MQTT `system/edge-daq/plc_status` 上报。
  - 当退避次数超过阈值时，向 `edge-monitor` 发出 `plc_disconnected` 事件，大屏同步展示并锁住控制参数（保持最近一次 setpoint）。
  - 计划引入 `plc_status` API，允许运维查看最近的连接错误、自动重连进度。

### 4.2 MQTT 连接

- **当前实现**：客户端依赖各自库自带的自动重连；`edge-monitor` 对 mosquitto 仅做 TCP 探测，未订阅 `$SYS/#`。
- **计划完善**：
  - 在 `common` 目录新增 `mqtt/connection_manager.py`，统一封装重连、心跳、TLS 配置。
  - `edge-monitor` 订阅 `$SYS/#` 与 `system/+/status`，当 Broker 状态异常或 retain 消息延迟时触发报警。
  - 所有服务的 MQTT handler 增加 “最大重连次数 + 通知 edge-monitor” 的回调，以便监控侧掌握具体客户端状态。

### 4.3 时序与业务数据库

- **当前实现**：`edge-monitor` 仅对 Influx/Postgres 做 HTTP/TCP 检测；`edge-api` 若依赖不可用只会抛异常，尚未进入只读模式。
- **计划完善**：
  - `edge-api` 增加数据库守护协程：定期 `SELECT 1`/写测试点失败时自动切换到只读模式，暂停写操作。
  - Influx 异常时启用本地缓存（暂存在文件/SQLite），待恢复后补写。
  - 将 “数据库状态” 通过事件总线推送到 `edge-monitor`，同步 `service_status` 为 `degraded/fail`。

---

## 5. AI 自愈机制

### 5.1 模型加载与回滚

- **当前实现**：`ai-softsensor` 只在启动时加载单一 ONNX 模型，若失败直接抛异常并终止；没有模型版本登记与回滚。
- **计划完善**：
  1. 在 PostgreSQL 增加 `ai_model_versions` 表，字段：`version`, `path`, `checksum`, `status`, `created_at`。
  2. `ai-config.yaml` 仅保存 “当前激活版本 ID”，`ai-softsensor` 启动时根据 ID 查表并加载模型、核对 checksum。
  3. 加载失败则回退到上一个 `status=stable` 的版本，并记录事件；若两次回退都失败，则标记服务 `fail` 并停机待人工处理。

### 5.2 推理结果校验与降级

- **当前实现**：没有范围/波动校验；推理异常仅写日志。
- **计划完善**：
  - 在推理 pipeline 中增加 `ResultGuard`，基于 `limits.yaml` 配置检查范围、delta、斜率。
  - 出现异常时按以下步骤降级：`warn → degraded（触发 MQTT 告警） → fallback_model（例如线性模型/经验公式） → fail（停止推理）`。
  - 降级状态通过 MQTT `ai/status` 发布，edge-api 在对外接口中体现当前模式。

---

## 6. OTA 自愈机制

### 6.1 升级流程关键点

- **当前实现**：
  - `scripts/ota-update.sh` 流程：备份 → `docker-compose pull` → `docker-compose up -d` → 检查 `docker-compose ps`。
  - 未启用影子容器/蓝绿发布，也未在通过前阻断对外服务。
- **计划完善**：
  - OTA 流程升级为 **蓝绿发布**：`pull -> docker compose up new stack (edge-api-blue)`，通过 `/health` & 冒烟脚本验证后，切换 `docker-compose.override.yml` 指向新 stack。
  - 在 `scripts/ota-update.sh` 中新增 `--operator` 参数，将操作人、版本、时间写入 `logs/ota-history.json` 并同步至 PostgreSQL。

### 6.2 失败检测与回滚

- **当前实现**：若脚本检测到 `unhealthy/exit` 容器，只提示人工执行 `rollback`。
- **计划完善**：
  1. 以 `edge-monitor` 收集到的系统状态为信号，若在观察窗口内出现 `fail` 或链路中断则自动判定升级失败。
  2. 失败时调用 `docker-compose -f docker-compose.blue.yml down` 并重新启用旧 stack，同时写入 `ota_history` 表与 MQTT 告警。
  3. 记录最新失败版本，防止自动任务再次升级到同版本。

---

## 7. 配置与策略管理

- **当前实现**
  - 与自愈相关的配置仅存在于 `configs/monitor.yaml`（采集频率、检测目标、容器名）。
  - `qos.yaml` 尚未描述自愈频率/重试策略，配置更新需手动修改文件并重启。
- **计划完善**
  - 新增 `configs/qos.yaml` 结构：
    ```yaml
    self_heal:
      default_failure_threshold: 3
      default_recovery_observe: 6
      default_max_restart_per_hour: 5
      notify_topics:
        - events/system/self_heal
    ```
    并允许 target 级别覆盖。
  - 构建 `config-service` 或在 PostgreSQL 增设 `config_changes` 表，所有配置变更需经 API / CLI 工具记录，并通过事件总线通知在线服务热加载。

---

## 8. 运维操作手册（摘要）

- **查看当前自愈状态**
  - 在大屏 / 运维工具中查看系统健康与自愈开关
- **暂时关闭某服务的自动重启**
  - 通过配置中心或运维 API，将该服务的自愈开关设为 `false`
- **手动触发自愈动作**
  - 例如：对特定服务进行“手动重启并观察”
- **查看自愈历史**
  - 从 InfluxDB / PostgreSQL 查询近期自愈事件和结果

---

## 9. 与安全与合规的关系

- 所有自愈动作必须有审计记录（谁、何时、因为什么触发）  
- 禁止未经授权的自动更新配置或模型  
- 在安全策略上，自愈不能绕过授权系统（如 `license-service` 失败时，AI 功能必须保持关闭）

---

## 10. 后续规划

- 引入 **策略引擎**：将自愈规则抽象为可配置规则集合（如 CEL / DSL）  
- 引入 **机器学习驱动的异常检测**：自动识别工艺异常与设备异常  
- 将自愈事件上报到云端统一监控平台（可选）


