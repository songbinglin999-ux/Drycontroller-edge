# Edge 架构 v2（分层重构）

> **版本**：v2.1-draft
> **日期**：2026-06-01
> **背景**：基于 5/30 级联故障复盘 + P0-SAFETY 对账结论 + 控制策略重新分层；2026-06-01 已合并 Codex / Claude 评审意见入正文。
> **状态**：目标架构 + 最终统一 e2e 验收口径。edge ↔ PLC 通讯不做主备/多链路冗余，保持单 Modbus RTU-over-TCP 链路 + 重连/超时/告警的最低复杂度实现。生产工作流为 `MANUAL` / `AUTO` 模式授权：六个变频频率目标均通过 D5100 单路事务进入 PLC，完整监督控制、PLC authority 和 HMI direct 预留统一纳入最终 e2e。

---

## 0. 设计原则

1. **存活优先**：远程通道和宿主机健康独立于业务容器，`docker compose down` 不应导致失联。
2. **PLC 是最后安全屏障**：edge 系统故障时 PLC 必须能独立维持安全运行，edge 不承担最终安全责任。
3. **最小容器数**：合并职责单一的微服务，减少编排复杂度和故障域。
4. **离线 7x24**：不依赖云端，本地闭环完整可运行。
5. **控制建议与控制命令分离**：AI 永不直接写 PLC；`edge-api` 是唯一 command issuer；`edge-daq` 只消费签名后的 command 并回 ACK/NAK。生产工作流按模式授权：`MANUAL` 下人工设频率目标，`AUTO` 下 edge 自动签发频率目标；edge-api 这一层（range guard / ramp-slew gate / 工艺联锁 / 签名 / 审计）始终不被旁路。
6. **保命层不依赖 tailnet 可用性**：LAN SSH + 防火墙默认 ACCEPT 是不可动摇的最终兜底，独立于 Tailscale 控制面是否可达。

---

## 1. 分层总览

```
┌─────────────────────────────────────────────────────────────┐
│                     保命层（宿主机级）                        │
│   Tailscale (systemd) ─ SSH (局域网) ─ host watchdog         │
└──────────────────────────┬──────────────────────────────────┘
┌──────────────────────────▼──────────────────────────────────┐
│                     基础层（数据基础设施）                     │
│   Postgres ─ Mosquitto ─ InfluxDB ─ Prometheus ─ Alertmanager│
└──────────────────────────┬──────────────────────────────────┘
┌──────────────────────────▼──────────────────────────────────┐
│                     业务层（应用服务）                        │
│   edge-api（含 license） ─ ai-softsensor ─ ai-ctrl-optimizer │
└──────────────────────────┬──────────────────────────────────┘
┌──────────────────────────▼──────────────────────────────────┐
│                     监控层（可观测性）                        │
│                    edge-monitor ─ ops-console                │
└──────────────────────────┬──────────────────────────────────┘
┌──────────────────────────▼──────────────────────────────────┐
│                     控制层（PLC 交互）                       │
│              edge-daq ─ PLC 本地闭环（PLC 侧）               │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. 保命层 — 宿主机级

**目标**：业务全挂时仍能远程接入排查。

| 组件 | 职责 | 实现方式 |
|---|---|---|
| Tailscale | 远程 VPN 通道 | **systemd 服务**，不在 Docker 容器内，独立于 compose 生命周期 |
| SSH | 局域网 fallback | 宿主机 sshd，Tailscale 不可达时走局域网接入 |
| host watchdog | 监控 Docker daemon | systemd unit（`Restart=always`）或独立脚本，Docker daemon 异常时自动重启；watchdog 自身由 systemd 守护，避免单点 |

**验收标准**：执行 `docker compose down` 后，通过 Tailscale SSH 仍可远程接入宿主机；通过 LAN SSH 同样可达。

**5/30 教训**：Tailscale 跑在 Docker 容器内，`compose down` 一起被杀，远程通道永久断联。此层直接解决该问题。

### 2.1 兜底底线（不可动摇）

tailnet 控制面走的是未备案域名，SNI 放行与 UPnP 端口映射都脆（重启路由器即丢），因此**保命层不能把"能新建 Tailscale 连接"当作前提**。最终兜底是 **LAN SSH(22) + 网卡/路由 + 防火墙默认 ACCEPT**，独立于 tailnet 可用性。任何安全收紧都不得切断这条 LAN 接入路径。

### 2.2 enroll 改造（不能标"不变"）

`edge-enroll` 当前依赖 compose 里的 `tailscale` 服务、共享 `tailscaled.sock` 和容器卷。Tailscale 迁宿主机 systemd 后，enroll 必须二选一改造，否则 Phase 1 做完设备能 SSH 但云端设备注册/心跳断掉：

- **宿主机 systemd oneshot/timer**：读取宿主机 `tailscale ip -4`，向 cloud-ops enroll。
- **容器保留 enroll**：只读挂载宿主机 tailscale socket，或经宿主机命令代理拿 tailnet IP。

---

## 3. 基础层 — 数据基础设施

**目标**：为上层提供可靠的数据存储和消息通道。

| 组件 | 职责 | 端口 |
|---|---|---|
| Postgres 15 | 业务配置、告警规则、审计日志、Alembic 迁移 | 127.0.0.1:5432 |
| Mosquitto 2.0 | MQTT 消息总线，4 用户隔离，ACL 控制，支持 TLS | 127.0.0.1:1883/8883 |
| InfluxDB 2.7 | 时序数据、AI 推理结果、事件记录 | 127.0.0.1:8086 |
| Prometheus | 指标采集，30 天留存 | 127.0.0.1:9090 |
| Alertmanager | 告警聚合 | 127.0.0.1:9093 |
| mosquitto-init | 一次性密码文件生成（run-once） | — |

**设计约束**：
- 所有端口默认绑定 `127.0.0.1`，不暴露工厂 LAN。
- 有状态服务使用 Docker volume 持久化。
- 无状态服务挂了数据不丢。

---

## 4. 业务层 — 应用服务

**目标**：数据采集、AI 推理、控制建议、API 网关。

### 4.1 edge-api（合并 license-service，唯一 command issuer）

| 职责 | 说明 |
|---|---|
| 统一 REST/WebSocket 网关 | 唯一对外入口 |
| 控制模式管理 | edge 模式 / 本地模式 / 模式切换 |
| **唯一 command issuer** | 按授权层级签发 **signed control command**（`MANUAL`：人工设六频率目标；`AUTO`：人工授权模式后自动签发）；两种层级都过 range guard + ramp/slew limiter + 工艺联锁 + 签名 + 审计，AI 不旁路（见 §6.5） |
| range guard / ramp / 工艺联锁 | 越界 setpoint 在签发前拒绝；合法 setpoint 按变化率缓变；工艺联锁不满足时标记 not_actionable |
| 授权校验 | 合并原 license-service 的 `/verify` 逻辑，减少一个容器 |
| Alembic 迁移 | 启动时自动执行 |
| 审计与 outbox | 控制命令、告警事件、command ACK/NAK 落库同步 |

**合并说明**：原 `license-service` 仅提供一个 `/verify` 接口，合并进 edge-api 后通过内部函数调用，减少一次网络跳转和一个容器编排。该合并**不是安全闭环的前置条件**（见 §9 Phase 5）。

### 4.2 ai-softsensor

| 职责 | 说明 |
|---|---|
| ONNX 推理 | 订阅 MQTT 实时点位，输出软测量含水率 |
| 结果发布 | MQTT + InfluxDB 双写 |

### 4.3 ai-ctrl-optimizer（输出"建议"，不直写 PLC）

| 职责 | 说明 |
|---|---|
| 控制建议计算 | 订阅实时数据 + AI 含水率，计算最优 setpoint |
| 输出形态 | 发布到**独立 suggestion topic/schema**，仅供 edge-api / ops-console 展示与决策；**绝不直达 edge-daq 或 PLC** |
| 模式感知 | PLC 处于本地模式时，suggestion 继续计算但标记 `not_actionable` 并记录，不进入 command 签发链路 |

**关键变化**：v1 中 ai-ctrl-optimizer 输出的 MQTT control command 由 edge-daq 直接写 PLC。v2 下它只产出 suggestion，能否变成 command 由 edge-api 的控制状态机、range guard、ramp/slew limiter、工艺联锁和 §6.5 授权协议裁决；PLC 只持有现场控制权锁与执行兜底，不做工艺推理。

---

## 5. 监控层 — 可观测性

**目标**：系统健康监控、自动自愈、运维可视化。

| 组件 | 职责 |
|---|---|
| edge-monitor | 服务探活（HTTP/TCP/容器状态）、自愈（阈值重启 + 熔断冷却）、PLC 模式状态监控、心跳健康监控、控制拒绝（NAK）可见、MQTT 状态发布、Alertmanager 集成 |
| ops-console | 本地运维大屏（Svelte SPA），展示实时数据、告警、系统状态、当前控制模式、授权层级（MANUAL/AUTO/AUTO_SUSPENDED）与 suggestion |

**edge-monitor 职责（相对 v1 新增）**：
- 监控 PLC 模式状态（edge 模式 vs 本地模式 vs 维护锁定），切换时触发告警。
- 监控 edge-daq 心跳：通讯心跳（`EDGE_HOST_ALIVE` / `PLC_HEARTBEAT`）和控制门控心跳（`EDGE_CONTROL_HEALTHY`，见 §6.1）。
- 控制命令被 PLC 拒绝（NAK）时可见并告警。
- 监控 AUTO 授权状态，AUTO 被自动挂起回 `AUTO_SUSPENDED` / `MANUAL` 时告警。

### 5.1 对外只读面鉴权分层

edge 对外（LAN / 大屏 / HMI）的 HTTP/WS 面按强度分两类，**不可共用一套鉴权**：只读面用来源白名单或绑定面，控制/运维面才用 HMAC / ops key。控制级握手套到只读流上是分层错配（曾经 `/ws/realtime` 与控制流共用 HMAC challenge-response，已纠正）。

| 面 | 端点（示例） | 鉴权 | 理由 |
|---|---|---|---|
| 只读面（edge-api） | `/ws/realtime`·`/ws/ai`·`/ws/system`、`/realtime/latest`·`/history`·`/system/health`、`v1/data`·`v1/ai`·`v1/alarms`(读)·`v1/system`(读) | 来源 IP 白名单（`require_readonly_*_source` → `READONLY_WS_ALLOWLIST`），**零凭证** | 同 LAN 只读，无需下发/管理凭证 |
| 只读面（edge-monitor） | `/system/health`·`/system/metrics`·`/health`·`/ready`·`/metrics` | 无应用层鉴权，靠主机端口绑定 + 防火墙（LAN/tailnet） | 纯只读快照，暴露面由网络层控制 |
| 控制/写入面（edge-api） | 控制 / signed command WS 与写入流 | 控制级 HMAC challenge-response + 签名 command | 写 PLC 执行器需防误写/防重放，与网络是否可信无关 |
| 运维操作面（edge-monitor） | `/ops/*`（self-heal / logs / alerts） | ops API key（`MONITOR_OPS_API_KEYS` / `ADMIN_API_KEYS`） | 触发重启 / 读日志属敏感操作 |

> 两个只读面用了**两种不同机制**（edge-api 应用层 IP 白名单 vs edge-monitor 绑定面 + 防火墙）。都可接受，但新增只读端点时必须明确归属哪一种，不要默认套控制握手。

**防回归（硬约束）**：新增任何只读 HTTP/WS 流，默认归只读面——来源白名单或绑定面 + 防火墙，**不得套控制级握手**；只有控制/写入流才用 HMAC + 签名 command，运维操作流才用 ops key。只读面与控制面共用一套鉴权即视为分层错配。

---

## 6. 控制层 — PLC 交互

**目标**：数据采集、双向通讯心跳、签名 command 下发、PLC 本地控制兜底，以及 HMI/edge 互斥控制权管理统一按最终 e2e 验收。

### 6.1 edge-daq

| 职责 | 说明 |
|---|---|
| 数据采集 | Modbus RTU-over-TCP 读取 PLC 寄存器/线圈，发布到 MQTT + InfluxDB |
| 通讯心跳 | 写 `EDGE_HOST_ALIVE`，读 `PLC_HEARTBEAT`；只用于通讯/数据质量诊断，不授权控制 |
| command 写入 | 只消费 edge-api 下发的**签名 command**，Modbus 写入 PLC，回 **ACK/NAK**；拒绝或写失败 NAK 且**不重放**危险写入 |
| 控制门控心跳写入 | **独立进程**，采集挂了心跳进程也要继续；控制路径不健康时 fail-closed |
| 模式/控制权回读 | 读取 PLC 模式、`AUTHORITY_OWNER`、`AUTHORITY_EPOCH`、`HMI_DIRECT_ENABLE` 和拒绝原因，上报到 MQTT |

**心跳语义拆分**（单一 `EDGE.HEARTBEAT` 不够；通讯心跳不得被 PLC 当成控制授权，控制门控心跳必须 fail-closed）：

- `EDGE_HOST_ALIVE`：edge 主机/采集运行时还活着，**仅用于通讯诊断**；PLC 不得据此允许 edge 控制。
- `PLC_HEARTBEAT`：PLC 自身通讯心跳，可复用现有 0/1 翻转镜像；edge 用短窗口确认读值是否变化，用于 PLC 通讯 freshness 和大屏/告警，避免 5s 单点采样与 1s 翻转周期同相。
- `EDGE_CONTROL_HEALTHY`（门控心跳）：仅当控制路径关键依赖（MQTT、edge-api、命令签名、range guard、ramp/slew limiter、工艺联锁、Modbus 写链路）全部健康、模式状态明确、最近一次写入/ACK 契约满足时才递增；任一不满足即 **fail-closed**（停止递增），让 PLC 切本地模式。PLC 的看门狗监测的是这个门控心跳。

本期不做 PLC 通讯冗余链路：不引入双 PLC host、双网口、RS485 备用、第二套心跳或主备切换状态机。链路异常只通过 Modbus 重连/熔断、双向心跳超时、告警和控制 fail-closed 处理，避免过度设计。

### 6.2 PLC 本地兜底 / 最终安全（PLC / VFD / 硬接线侧实现）

| 功能 | 说明 |
|---|---|
| 通讯心跳 | PLC 更新/翻转 `PLC_HEARTBEAT` 并接收 edge 递增写入的 `EDGE_HOST_ALIVE`；edge 对 PLC 翻转心跳做短窗口确认采样，只用于确认通讯和采集链路，不触发模式切换 |
| 心跳看门狗 | 监测 `EDGE_CONTROL_HEALTHY` 寄存器，M 秒未更新判定 edge 控制不可信 |
| 模式切换 | 门控心跳正常 → `EDGE_MASTER`；超时 → `PLC_LOCAL` 或 `HMI_LOCAL`（预设安全 setpoint / 本地保守 SP） |
| **控制权锁** | PLC 持有 `AUTHORITY_OWNER` 与 `AUTHORITY_EPOCH`；edge 和 HMI 都只能申请控制权，不能绕过 PLC 私下切换；现场内网不引入 token/签名租约 |
| **HMI direct 预留** | 当前只保留状态预留，不作为 DryController 六频率写入路径；DryController HMI 的六频率输入仍经 edge-api / D5100 事务进入 PLC |
| **维护锁定（maintenance lock）** | 人为锁定本地模式，需**手动解除**后才允许 edge 重新接管；**在 PLC 侧实现**，否则维修中 edge 心跳恢复仍可能强行夺回控制权 |
| bumpless 切换 | 模式切换时输出不能跳变，平滑过渡；正常工艺 ramp/slew 仍由 edge 控制状态机负责 |
| actuator final clamp | PLC/VFD 对已收到的输出做最后物理限幅兜底；正常工艺限幅由 edge-api 签发前完成 |
| 急停/硬安全联锁 | 硬件级安全，edge 不介入；工艺联锁由 edge 控制状态机负责 |
| 上电默认 | 上电后进入安全默认状态 |
| DO 触点告警 | 心跳超时 → DO 闭合 → DTU 短信告警（**独立于 edge，独立于网络**） |
| 模式回读寄存器 | edge 可读当前模式、控制权 owner、epoch、HMI 直连授权、接管/拒绝原因 |

### 6.3 控制模式切换流程

```
          门控心跳正常                         门控心跳超时 / edge 不可用
┌──────────────────┐    ┌──────────┐    ┌────────────────────┐
│ EDGE_MASTER       │ →  │ 等待确认  │ →  │ PLC_LOCAL/HMI_LOCAL │
│ owner=EDGE        │    │ (防抖)    │    │ owner=PLC/HMI       │
└──────────────────┘    └──────────┘    └────────────────────┘
      ↑                                               │
      └──── edge 恢复后读取当前状态、同步 SP、申请 epoch ──┘

         HMI_STANDBY：edge 健康时 HMI 只监视/请求接管，不直接写控制 SP
         HMI_DIRECT_ENABLE：仅预留状态；当前不启用 HMI->PLC 六频率直连写入
         维护锁定：人为置入 → 强制本地模式 → 仅手动解除可退出
         （锁定期间忽略 edge 心跳恢复，不自动接管）
```

**防抖**：连续 K 次门控心跳丢失才判定故障，避免偶发丢包误切。

**恢复原则**：edge 恢复后不能自动抢回控制权。它必须先读取 PLC 当前实际输出、有效 setpoint、`AUTHORITY_OWNER` 和 `AUTHORITY_EPOCH`，完成参数/状态同步后再请求 `EDGE_MASTER`。PLC 授权前，edge-api 应拒绝新 control command 或标记为 `not_actionable`。

### 6.4 PLC → 云端告警链路

```
正常路径：PLC → Modbus → edge-daq → MQTT → edge-api → cloud-ops
独立路径：PLC → DO 触点 → 4G DTU → 短信 → 运维手机
```

独立路径在 edge 全挂时仍可工作。

### 6.5 控制建议 / 控制命令协议（契约）

| 环节 | 约定 |
|---|---|
| suggestion 发布 | ai-ctrl-optimizer 发布到独立 topic/schema，仅进入 edge-api / ops-console 展示；**绝不直达 PLC** |
| suggestion → command | 由 **edge-api** 转为 signed control command；生产模式下不逐条人工确认，是否自动签发取决于 `MANUAL` / `AUTO` 模式授权；AI 不参与签发 |
| command 消费 | edge-daq 只接受签名 command，Modbus 写入后回 ACK/NAK |
| PLC 拒绝 | 拒绝原因可回读、审计、上报；NAK 不触发危险重放 |
| 本地模式行为 | suggestion 标记 `not_actionable` 并记录，不进入签发链路 |

**授权层级**（无论哪一级，edge-api 的 range guard / ramp-slew gate / 工艺联锁 / 签名 / 审计都不被旁路，AI 始终不直接写 PLC）：

- **MANUAL（默认）**：操作员直接设六个频率目标，edge-api 只在 gate 通过后签发 command；它不是绕过安全链路。
- **AUTO**：操作员显式授权后，edge-api 自动把规则/算法 suggestion 转签名 command 下发，由 PLC 侧 clamp / 本地模式 / 心跳门控 / 急停兜底。约束：
  - **需人工显式授权开启**，默认不开；
  - 无人值守 AUTO 只负责自动生成单路频率任务，仍复用 edge-daq 的 D5100 队列；同一时刻只允许一个 in-flight 请求，上一条 ACK/NAK/超时处理完成后才发下一条；
  - 当前仅允许六个频率目标；6SP 地址/倍率/反馈/ACK-NAK/读回/安全覆盖状态签收前必须 fail-closed；
  - edge HMI 人工修改任一频率目标时，立即退回 **MANUAL**，停止 AUTO 生成新任务，并丢弃/冻结尚未下发的 AUTO 任务；该人工目标仍按 `mode=MANUAL` 走同一条 D5100 单路事务；
  - PLC 本地屏/旧屏/维护操作接管时，PLC 必须通过 owner/state/local-manual 信号暴露；edge 读到后进入 **AUTO_SUSPENDED** 或 MANUAL 展示态，停止写 D5100，避免和现场本地操作抢控制权；
  - 授权一直有效，**直到运维手动退回 MANUAL**、人工调频、PLC 本地接管或 gate 失败；
  - 进入本地模式 / 维护锁定 / 任一控制依赖 fail-closed 时自动进入 **AUTO_SUSPENDED**，停止新 command；恢复后需重新人工授权才能再次进入 AUTO。

---

## 7. 服务清单对比

| 服务 | v1（当前） | v2（新架构） | 变更 |
|---|---|---|---|
| edge-api | ✓ | ✓ | 合并 license-service，成为唯一 command issuer |
| edge-daq | ✓ | ✓ | 新增通讯心跳、控制门控心跳、模式回读和签名 command/ACK |
| ai-softsensor | ✓ | ✓ | 不变 |
| ai-ctrl-optimizer | ✓ | ✓ | 输出从"命令"改"建议"，不直写 PLC |
| license-service | ✓ | ✗ | 合并进 edge-api |
| edge-monitor | ✓ | ✓ | 新增 PLC 模式 / 心跳健康 / NAK 监控 |
| ops-console | △（代码已存在，未接 compose） | ✓ | edge 本地维护控制台（Svelte/Vite，`ops-console/`），面向维护工程师，走 edge-api 管理员 API Key；当前无 Dockerfile/未接 compose，**部署形态待定**（见下） |
| edge-ota-agent | ✓（profile: ota） | ✓ | 不变 |
| postgres | ✓ | ✓ | 不变 |
| influxdb | ✓ | ✓ | 不变 |
| mosquitto | ✓ | ✓ | 不变 |
| mosquitto-init | ✓ | ✓ | 不变 |
| prometheus | ✓ | ✓ | 不变 |
| alertmanager | ✓ | ✓ | 不变 |
| docker-socket-proxy | ✓ | ✓ | 不变 |
| docker-socket-proxy-ota | ✓ | ✓（profile: ota） | 不变 |
| tailscale | ✓（profile: remote-debug，容器内） | ✗（迁 **systemd**，宿主机级） | 迁出容器 |
| edge-enroll | ✓（profile: remote-debug） | ✓ | **需改造**（Tailscale 迁出后激活链路调整，见 §2.2） |
| Grafana | — | **不加** | — |

**容器数推导**：v1 compose 实测 **17 个 service**（已核实）。v2 = 17 − license-service（−1）− tailscale 迁 systemd（−1）= **15**；若 ops-console 落为 edge compose 内独立容器则 +1 = **16**。

**ops-console 部署形态（推荐 edge-api StaticFiles 挂卷托管，容器数 15）**：身份与边界已由 `ops-console/README.md` 明确——edge 本地维护控制台（维护工程师用，可重启服务/看日志/调自愈参数，走 edge-api 管理员 API Key），区别于现场只读大屏（`Drycontroller-frontend`/bigscreen）和云端 cloud-console（多节点/租户/license/OTA/job）。代码已存在但未接 compose，落地排期靠后（见 §9 Phase 6）。形态对比：
- **edge-api StaticFiles 挂卷托管（推荐）**：`npm run build` 产物（`ops-console/dist`）经宿主机目录只读挂进 edge-api，挂载在 `/console` 子路径，与 `/api/v1` 同源 → 零 CORS、WebSocket 同源；不新增容器，容器数 **15**；前端更新只替换挂卷产物，不重建 edge-api 镜像。
- **独立 nginx 容器**：单独静态服务容器 → 新增 1 容器（**16**），且需反代 `/api` 或开 CORS；前后端发布解耦在单人开发下无实际收益，不选。

> 注：基础层（§3）无 web server，"基础层 nginx 托管不新增容器"不成立——用 nginx 必然 +1；只有 edge-api 托管才能保持 15。

---

## 8. 与 P0-SAFETY 的对齐

P0-SAFETY 在 2026-06-15 后按新责任边界推进：edge 负责工艺控制落地、控制状态机、软点策略、range guard、setpoint ramp/slew limiter、工艺联锁和软件侧安全兜底；PLC 负责软点寄存器/执行接口、参数同步、报警同步、状态同步、执行、采集、ACK/NAK/原因码、无扰切换、HMI/edge 控制权锁和心跳丢失切本地；VFD/硬接线/设备自保护保留最终物理安全兜底。本架构直接覆盖：

> **最终统一口径**：只读采集寄存器、`PLC_HEARTBEAT`、`EDGE_HOST_ALIVE`、六个频率目标软点的地址/量程/写入/反馈/PLC 安全覆盖状态，以及 edge-api 的 `MANUAL` / `AUTO` 模式授权、白名单、范围、变化率、冷却、ACK/NAK、读回和审计链路，统一进入最终 e2e；未签收的控制写入保持 disabled。

| P0-SAFETY 要求 | 本架构对应 |
|---|---|
| PLC 软点控制层 | 6.2 PLC 本地控制兜底 + 6.3 控制模式切换流程 |
| 参数同步 / 报警同步 / 状态同步 | 6.3 控制模式切换流程 + Phase 2 接口签收 |
| 通讯心跳 | 6.1 `EDGE_HOST_ALIVE` + `PLC_HEARTBEAT`，只用于通讯诊断 |
| 控制门控心跳与心跳丢失切本地 | 6.1 `EDGE_CONTROL_HEALTHY` 门控心跳（fail-closed） |
| ACK/NAK 与原因码 | 4.1 edge-api 签发/审计 + Phase 3 ACK/NAK |
| 无扰切换 / HMI-edge 控制权锁 | 6.2 PLC 本地闭环 + 6.3 控制模式切换流程 |
| HMI direct 预留状态 | 6.2 `HMI_DIRECT_ENABLE` + 6.3 `HMI_STANDBY/HMI_LOCAL`，当前不作为六频率写入路径 |
| edge 侧补心跳 | 6.1 通讯心跳 + 控制门控心跳（独立进程） |
| edge 侧补 range guard / ramp / 工艺联锁 | 4.1 edge-api（签发前拒绝越界、按变化率缓变、联锁不满足时拒绝） |
| edge 侧补控制状态机和软件侧 fallback | 4.1 edge-api + 6.3 控制模式切换流程（含维护锁定） |
| edge 侧补 `/control/estop` | §9 Phase 3（edge 控制契约）落位 |
| AUTO 启用门槛 | §6.5：6SP 地址/倍率/反馈/ACK-NAK/读回/安全覆盖状态签收前 fail-closed；完整监督控制统一进入最终 e2e 验收 |

---

## 9. 推进范围

当前不再按 V0/V1/V2/V3 做分期验收。开发只保留最终统一版本：PLC 数据链路、双向心跳、六个变频频率目标、D5100 单路频率事务、ACK/NAK、控制权、HMI direct 预留、参数/报警/过程约束和 edge MANUAL/AUTO 门控统一进入最终 e2e。除已签收的六个频率目标外，其余控制写入仍默认 disabled，直到对应 PLC/edge 契约在最终 e2e 中跑通。

### 9.1 最终统一控制能力

| 范围 | 定位 | PLC 边界 | Edge 边界 |
|---|---|---|---|
| 最终统一版本 | PLC 接入、数据质量、双向心跳、六个频率目标和后续监督控制能力统一验收 | PLC 提供 28 个 DryController D 字读点 + 6 个料位/限位线圈、`PLC_HEARTBEAT`、`EDGE_HOST_ALIVE`、D5100 单路频率事务、PLC 有效目标、PLC 输出命令/MV、PV/status invalid/not-wired 标记、运行/故障/料位/安全覆盖状态；`D30-D35` 原工程频率链路不进入 DryController 采集/控制路径；真实 VFD PV/status 待反馈线或通讯补齐后另签 | edge 管“谁能写、写多少、何时写”：只读采集、工程量换算、基础统计、阈值告警、大屏、本地历史、MANUAL/AUTO 模式授权、频率软点白名单、D5100 队列写入、限幅、变化率、冷却、审计、可解释建议和自动挂起 |

### 9.2 工程事项

| 事项 | 内容 | 验收标准 |
|---|---|---|
| 数据链路 | PLC tags、采集、工程量换算、阈值告警、大屏、`PLC_HEARTBEAT` 读取、`EDGE_HOST_ALIVE` 写入 | 现场点表落库；只读数据链路和双向通讯心跳 E2E 跑通 |
| 保命层 | 宿主机 Tailscale + LAN SSH + host watchdog + enroll 改造 | `compose down` 后 Tailscale/LAN SSH 均可达；cloud-ops 仍能识别设备 tailnet 地址 |
| PLC 统一接口 | 采集寄存器、D5100 单路频率事务、参数同步、报警同步、状态同步、ACK/NAK、心跳切本地、HMI direct 预留、无扰切换、HMI/edge 控制权锁、最终物理安全兜底回读 | 停止 edge 心跳后 PLC 自动切本地；频率写入只走 edge-api / D5100；软点写入有 ACK/NAK 与原因码；控制权切换无扰且 owner/epoch 状态可见；VFD/硬接线/设备保护可现场验收 |
| edge 控制契约 | suggestion schema、signed command、ACK/NAK、range guard、setpoint ramp/slew limiter、工艺联锁、`/control/estop`、fallback 状态机、`MANUAL` / `AUTO` / `AUTO_SUSPENDED` 授权机制、owner/epoch gate | AI 不直写 PLC；未签名/越界/变化率超限/工艺联锁不满足/本地模式/心跳异常/非 EDGE owner/stale epoch 下 command 被拒并审计；AUTO 可一键退回 MANUAL，gate 失败自动挂起 |
| 监控层 | PLC 模式监控、控制权 owner/epoch、HMI 直连授权、心跳健康、NAK 告警、大屏状态 | 模式切换、心跳超时、HMI 接管、控制拒绝均可见且告警 |
| license-service 合并 + 文档收敛 | 功能等价测试通过；旧端点/云端探测路径有兼容或迁移说明；文档与代码一致 | 功能等价测试通过，文档与代码一致 |
| ops-console 接入 | edge-api StaticFiles 挂卷托管（见 §7）+ 鉴权整改（代码已存在，落地排期靠后） | `dist` 挂卷只读托管于 edge-api `/console`，容器数维持 15；整改 `VITE_ADMIN_API_KEY` 把长期管理员 Key 编译进前端静态产物的泄密隐患（改登录换短期 token，不再把长期 Key 打包进 JS）；维护工程师可访问，实时数据 / 告警 / 控制模式 / 授权层级 / suggestion 可见 |

---

## 10. 不变的部分

以下设计在新架构中保持不变：

- Headscale tailnet 隔离运维通道，PLC 不暴露公网。
- degraded 状态机完整，edge 依赖异常时降级而非直接退出。
- PLC 控制 idempotency window、MQTT/Influx 指数退避。
- JWT secret 空值 fail-closed、API key 分级。
- Job/JobItem 状态机。
- 所有主机端口默认绑定 `127.0.0.1`。
- 对外只读面（数据/健康）走来源白名单或绑定面 + 防火墙、零控制凭证；控制/写入面才用 HMAC + 签名 command，运维操作面才用 ops key（见 §5.1）。
- Mosquitto 禁匿名、ACL 隔离。
- 自愈机制：阈值触发 + 冷却 + 熔断升级。

---

## 11. 失败注入验收

验收不能只覆盖正常路径，至少覆盖以下故障场景：

| 场景 | 预期 |
|---|---|
| `docker compose down` | Tailscale/LAN SSH 仍可达；PLC 切本地或保持安全状态；云端显示业务离线但主机可达 |
| kill `edge-daq` 主进程 | 门控心跳停止；PLC 防抖后切本地；edge-monitor/Alertmanager 告警 |
| kill 心跳进程 | PLC 防抖后切本地；采集即使还在也不能保持 edge 模式 |
| MQTT 不可用 | 门控心跳 fail-closed；不产生新可执行 command；状态可审计 |
| InfluxDB 不可用 | 采集链路降级缓存；控制路径不因时序库异常失控 |
| edge-api 不可用 | 无新 command 下发；PLC 不依赖 edge-api 维持安全 |
| Modbus 写失败 | command NAK；不重放危险写入；告警可见 |
| PLC 切本地模式 | suggestion 标 `not_actionable`；edge-api 拒绝控制写入 |
| HMI 接管 | PLC owner 切为 HMI/PLC_LOCAL 并授予直连；edge-api 拒绝控制写入；edge 恢复后只能申请接管 |
| stale authority token | PLC NAK；edge-api 审计为控制权过期，不重放 |
| 越界 / 变化率超限 setpoint | edge-api range guard / ramp-slew gate 拒绝或缓变；edge-daq 二次拒绝非法值；PLC/VFD final clamp 兜底 |
| edge HMI 人工调频 | 立即退回 `MANUAL`，停止 AUTO 任务生成；人工目标按 D5100 单路队列下发 |
| PLC 本地屏 / 旧屏人工调频 | 依赖 PLC owner/state/local-manual 信号；edge 进入 `AUTO_SUSPENDED` 或 MANUAL 展示态，停止写 D5100 |
| 维护锁定期间 edge 恢复 | PLC 不自动接管，需手动解除 |
| AUTO 运行中 PLC 切本地 / 依赖 fail-closed | 自动进入 `AUTO_SUSPENDED` / `MANUAL`；恢复后需重新人工授权 |
| 宿主机重启 | Tailscale、SSH、watchdog、业务 compose 按序恢复，无需现场介入 |

---

## 12. 需同步的文档

v2 被采纳后须持续同步以下文档，避免团队按旧链路继续实现：

- `docs/ops/remote-debug-dev.md`：已改为宿主机 Tailscale / Headscale 运维说明。
- `docs/archive/ops/provisioning-bootstrap.md`：旧 Provisioning USB 路径已归档，当前激活流程以 OS Installer USB + host tailnet / activation service 为准。
- `docs/architecture/data-flow.md`：已同步为 suggestion -> edge-api signed command -> edge-daq。
- `docs/architecture/control-strategy.md`：已同步为 suggestion / command 双协议。
- `docs/architecture/system-overview.md`：已同步 edge-daq command 消费与 ai-ctrl-optimizer 不直写 PLC；license-service 合并仍是后续 phase。
- `docs/ops/后端参数配置说明.md`：license-service、Tailscale、控制 topic/ACK 参数都会受影响。
