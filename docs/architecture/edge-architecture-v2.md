# Edge 架构 v2（分层重构）

> 2026-09-19：控制设计按根活动项 PLC-EDGE-SINGLE-CONTROL-20260918 修订为 Edge在线主控、HMI2离线直连PLC兜底；本次更新控制边界及相关验收。其他历史分层说明不构成新实施范围，现役部署与门禁以活动审计为准。

> **版本**：v2.1-draft
> **日期**：2026-06-01
> **背景**：基于 5/30 级联故障复盘 + P0-SAFETY 对账结论 + 控制策略重新分层；2026-06-01 已合并 Codex / Claude 评审意见入正文。
> **状态**：2026-09-19控制设计修订；现场Edge在线时为主控制仲裁入口，PLC保留最终保护/顺控/命令有效性；Edge不可用时HMI2需要直连PLC读写的现场应急能力。现场已证标准Modbus TCP；专用屏代理不是热备冗余，HMI2直连兜底另验。地址/倍率/启停/调频接口待当前工程签收，未开放生产写入。

---

## 0. 设计原则

1. **存活优先**：远程通道和宿主机健康独立于业务容器，`docker compose down` 不应导致失联。
2. **PLC 是最后安全屏障**：edge 系统故障时 PLC 必须能独立维持安全运行，edge 不承担最终安全责任。
3. **最小容器数**：合并职责单一的微服务，减少编排复杂度和故障域。
4. **离线 7x24**：不依赖云端，本地闭环完整可运行。
5. **控制建议与控制命令分离**：AI 永不直接写 PLC；Edge在线时 `edge-api` 是主 command issuer，`edge-daq` 只消费签名后的 command 并回 ACK/NAK。生产工作流按模式授权：`MANUAL` 下人工设频率目标，`AUTO` 下 edge 自动签发频率目标；edge-api 这一层（range guard / ramp-slew gate / 工艺联锁 / 签名 / 审计）在线主路径不被旁路。Edge不可用时，HMI2直连PLC是现场人工应急路径，不启用AI/AUTO。
6. **维护入口最小授权**：当前只验收腾讯云专用 `tag:ops` 跳板经 Tailscale SSH 以 `dryops` 登录 Edge，ACL 仅允许 `tag:ops -> tag:edge:22`；LAN operations-CA 入口属于后续独立 DANGER gate，不计入当前验收。

---

## 1. 分层总览

```
┌─────────────────────────────────────────────────────────────┐
│                     保命层（宿主机级）                        │
│   Tailscale (systemd) ─ Tailscale SSH ─ host watchdog        │
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
| SSH | 远程维护入口 | 腾讯云专用 `tag:ops` 跳板经 Tailscale SSH，以 `dryops` 登录；不下发 Edge 私钥或默认密码 |
| host watchdog | 监控 Docker daemon | systemd unit（`Restart=always`）或独立脚本，Docker daemon 异常时自动重启；watchdog 自身由 systemd 守护，避免单点 |

**验收标准**：执行 `docker compose down` 后，腾讯云专用 `tag:ops` 跳板仍可通过 Tailscale SSH 以 `dryops` 接入宿主机，且不能访问 Edge `8000`。

**5/30 教训**：Tailscale 跑在 Docker 容器内，`compose down` 一起被杀，远程通道永久断联。此层直接解决该问题。

### 2.1 当前维护边界

当前上线范围只有腾讯云专用 `tag:ops` 跳板到 `tag:edge:22` 的 Tailscale SSH；Edge 在受限 edge-api ingress 生效并校验节点身份后才启用 SSH。不得把 LAN SSH、默认 ACCEPT、Edge 密码库或通用远程执行写入本阶段验收。基于 operations-CA 的 LAN 应急入口如需落地，必须作为后续独立 DANGER 变更重新设计、复核和验收。

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

### 4.1 edge-api（进程内 License admission，唯一 command issuer）

| 职责 | 说明 |
|---|---|
| 统一 REST/WebSocket 网关 | 唯一对外入口 |
| 控制模式管理 | edge 模式 / 本地模式 / 模式切换 |
| **唯一 command issuer** | 按授权层级签发 **signed control command**（`MANUAL`：人工设六频率目标；`AUTO`：人工授权模式后自动签发）；两种层级都过 range guard + ramp/slew limiter + 工艺联锁 + 签名 + 审计，AI 不旁路（见 §6） |
| range guard / ramp / 工艺联锁 | 越界 setpoint 在签发前拒绝；合法 setpoint 按变化率缓变；工艺联锁不满足时标记 not_actionable |
| 授权校验 | 进程内执行 TPM-bound License verifier，不存在独立授权容器 |
| Alembic 迁移 | 启动时自动执行 |
| 审计与 outbox | 控制命令、告警事件、command ACK/NAK 落库同步 |

**合并说明**：原独立授权容器已删除。edge-api 只读消费 License、trust policy、Device identity 与 live TPM observation，通过内部函数完成验签；不存在网络代理或第二套授权执行面。

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

**关键变化**：v1 中 ai-ctrl-optimizer 输出的 MQTT control command 由 edge-daq 直接写 PLC。v2 下它只产出 suggestion，能否变成 command 由 edge-api 的控制状态机、range guard、ramp/slew limiter、工艺联锁和 §6 的Edge在线主控规则裁决；PLC保留本地/检修限制、设备保护与执行。HMI2离线直连兜底不启用AI/AUTO。

---

## 5. 监控层 — 可观测性

**目标**：系统健康监控、自动自愈、运维可视化。

| 组件 | 职责 |
|---|---|
| edge-monitor | 服务探活（HTTP/TCP/容器状态）、自愈（阈值重启 + 熔断冷却）、PLC 模式状态监控、心跳健康监控、控制拒绝（NAK）可见、MQTT 状态发布、Alertmanager 集成 |
| ops-console | 本地运维大屏（Svelte SPA），展示实时数据、告警、系统状态、当前控制模式、授权层级（MANUAL/AUTO/AUTO_PAUSED）与 suggestion |

**edge-monitor 职责（相对 v1 新增）**：
- 监控 PLC 模式状态（edge 模式 vs 本地模式 vs 维护锁定），切换时触发告警。
- 监控 edge-daq 心跳：通讯心跳（`EDGE_HOST_ALIVE` / `PLC_HEARTBEAT`）。当前 PLC gate 只使用 `D5291 EDGE_HOST_ALIVE`，不启用独立 `D5105` 控制健康 gate。
- 控制命令被 PLC 拒绝（NAK）时可见并告警。
- 监控 AUTO 授权状态，AUTO 被自动挂起回 `AUTO_PAUSED` / `MANUAL` 时告警。

### 5.1 对外只读面鉴权分层

edge 对外（LAN / 大屏 / HMI）的 HTTP/WS 面按强度分两类，**不可共用一套鉴权**：只读面用来源白名单或绑定面，控制/运维面才用 HMAC / ops key。控制级握手套到只读流上是分层错配（曾经 `/ws/realtime` 与控制流共用 HMAC challenge-response，已纠正）。

| 面 | 端点（示例） | 鉴权 | 理由 |
|---|---|---|---|
| 只读面（edge-api） | `/ws/realtime`·`/ws/ai`·`/ws/system`、`/system/health`、`v1/data`·`v1/ai`·`v1/alarms`(读)·`v1/system`(读) | 来源 IP 白名单（`require_readonly_*_source` → `READONLY_WS_ALLOWLIST`），**零凭证** | 同 LAN 只读，无需下发/管理凭证 |
| 只读面（edge-monitor） | `/system/health`·`/system/metrics`·`/health`·`/ready`·`/metrics` | 无应用层鉴权，靠主机端口绑定 + 防火墙（LAN/tailnet） | 纯只读快照，暴露面由网络层控制 |
| 控制/写入面（edge-api） | 控制 / signed command WS 与写入流 | 控制级 HMAC challenge-response + 签名 command | 写 PLC 执行器需防误写/防重放，与网络是否可信无关 |
| 运维操作面（edge-monitor） | `/ops/*`（self-heal / logs / alerts） | ops API key（`MONITOR_OPS_API_KEYS` / `ADMIN_API_KEYS`） | 触发重启 / 读日志属敏感操作 |

> 两个只读面用了**两种不同机制**（edge-api 应用层 IP 白名单 vs edge-monitor 绑定面 + 防火墙）。都可接受，但新增只读端点时必须明确归属哪一种，不要默认套控制握手。

**防回归（硬约束）**：新增任何只读 HTTP/WS 流，默认归只读面——来源白名单或绑定面 + 防火墙，**不得套控制级握手**；只有控制/写入流才用 HMAC + 签名 command，运维操作流才用 ops key。只读面与控制面共用一套鉴权即视为分层错配。

---

## 6. 控制层 — PLC 交互

2026-09-19采用Edge在线主控、HMI2离线直连PLC兜底。Edge在线时，专用PLC屏与HMI2发操作意图到Edge；HMI2仍经hmi-runtime访问Edge。来源权限、人工/自动和冲突统一由edge-api处理，DAQ仅执行可信命令；专用屏不得绕过Edge直写PLC。

Edge/API/DAQ不可用且排他执行许可、本地授权、持久日志和未知结果锁均成立时，HMI2进入应急直连PLC模式，读写核定PLC点位；未能证明Edge写路径已隔离时只读。Edge恢复后先只读观察，现场人工交回后才冻结HMI2新写、核对日志/未知结果并撤销直连许可；不自动恢复AUTO或覆盖HMI2应急写入。PLC保留本地/检修限制、顺控、正反转/星角互锁、设备保护、最终限幅、命令有效性/防重放和失联处置。旧D5400多来源owner/epoch和各来源缓冲不自动恢复；HMI direct能力目标恢复为HMI2离线兜底，但地址/授权/ACK和回切按当前工程重核。现役代码门禁不在本轮删除。

### 6.1 数据与命令链

- 通讯：标准Modbus TCP（示例 `192.0.2.200:2000` / unit 1），见 [通讯参数说明](../plc/plc-comm-protocol.md)。
- 常驻只读：M1001 / FC01 / 31001已验证；其他工艺值/写点不能从通讯成功外推。
- 命令：来源身份/权限 → 模式与白名单/范围/时序检查 → 既有可信签发/DAQ执行 → PLC接受或拒绝 → 输出/独立反馈。
- 结果未知不自动重发，不把Modbus回显当设备运行；原始输出与VFD真实反馈分开。

### 6.2 失联与恢复

Edge在线主路径持有执行许可时，专用屏和HMI2操作经Edge签发。Edge/API/DAQ不可用时，HMI2只有取得PLC或核定硬件/网络切换机构提供的排他执行许可后才可直连写PLC；未取得许可时保持只读，避免HMI2到Edge的局部分区与仍在运行的DAQ形成双写。PLC失联动作和超时按设备/工艺签收，不预设全停或hold-last；急停和必要本地停机独立保留。M1001只证明PLC侧活跃，不能代替PLC对Edge命令会话的失效检测。

重连先只读观察PLC和HMI2直连状态，不自动冻结或撤销仍在现场使用的HMI2应急权限。现场人工发起交回后，才冻结HMI2新写，核对HMI2直连日志、在途/未知结果和排他许可撤销，再读PLC状态、清理旧意图、重新建立有效会话；不重放启动/调频，AUTO须人工重新授权。旧D5291 watchdog等仅是旧设计/代码记录，不冒充现场已实现。

PLC维护锁仍在PLC侧锁存、仅人工解除，网络/心跳恢复不解锁。正常模式切换和重连保持无扰衔接，保护动作优先。人工调频先确认来源权限，立即转MANUAL、停止生成AUTO并丢弃/冻结未发AUTO；查清已在途结果后再发送人工命令，同一PLC链只允许一个在途请求，避免旧AUTO覆盖人工值。单次/累计变化率、冷却和工艺约束继续保留。

### 6.3 当前清单与接口缺口

点位、用途订正和弃用状态只维护于[I/O 点表](../plc/io-map.html)：19项启停用途/21个M候选，4个启动参数；17个M只有隔离置1→M/Y读回证据。没有生产控制验收点。

D30–D35未确认为可直写SP，D5100–D5699旧整包不是新方案强制前提。最小启停/调频/参数接口须基于当前梯形图核定并保留保护、防重放、拒绝与读回语义；未签收期间锁写，不保留新旧并行生产路径。

### 6.4 报警与自动策略

报警/状态继续沿PLC→Edge→HMI/Cloud读取链传递；源数据未知则显示未知。工艺调节归Edge，第一版可先人工操作，自动算法另验；不扩展Cloud远程启停、AI服务或VFD通讯范围。

---

## 7. 服务清单对比

| 服务 | 旧 v1 | 当前架构 | 变更 |
|---|---|---|---|
| edge-api | ✓ | ✓ | 内置 License admission，成为唯一 command issuer |
| edge-daq | ✓ | ✓ | 新增通讯心跳、authority 回读和签名 command/ACK；D5100 writer 仍由开关 fail-closed |
| ai-softsensor | ✓ | ✗ | 服务源码已从活动仓删除；恢复须由根活动审计重新授权 |
| ai-ctrl-optimizer | ✓ | ✗ | 服务源码已从活动仓删除；恢复须由根活动审计重新授权 |
| 独立授权容器 | ✓ | ✗ | 已删除；验签合并进 edge-api |
| edge-monitor | ✓ | ✓ | 新增 PLC 模式 / 心跳健康 / NAK 监控 |
| ops-console | △（代码已存在，未接 compose） | ✓ | edge 本地维护控制台（Svelte/Vite，`ops-console/`），面向维护工程师，走 edge-api 管理员 API Key；当前无 Dockerfile/未接 compose，**部署形态待定**（见下） |
| drycontroller-ota-helper.service | ✗ | ✓（systemd） | root-owned UDS；只接受 operation ID |
| postgres | ✓ | ✓ | 不变 |
| influxdb | ✓ | ✓ | 不变 |
| mosquitto | ✓ | ✓ | 不变 |
| mosquitto-init | ✓ | ✓ | 不变 |
| prometheus | ✓ | ✓ | 不变 |
| alertmanager | ✓ | ✓ | 不变 |
| docker-socket-proxy | ✓ | ✗ | RB-3 删除；业务容器不再访问 Docker API |
| docker-socket-proxy-ota | ✓（profile: ota） | ✗ | RB-3 删除；由 root-owned helper 替代 |
| tailscale | ✓（profile: remote-debug，容器内） | ✗（迁 **systemd**，宿主机级） | 迁出容器 |
| edge-enroll | ✓（profile: remote-debug） | ✓ | **需改造**（Tailscale 迁出后激活链路调整，见 §2.2） |
| Grafana | — | **不加** | — |

**当前 Compose 计数**：`docker-compose.yml` 默认服务共 **9** 个；独立授权容器和两个 AI 服务均不在运行集合，Tailscale 与 OTA helper 属于宿主 systemd 边界。应用 release 精确只有 `edge-api` + `edge-daq`。

**ops-console 部署形态（推荐 edge-api StaticFiles 挂卷托管，容器数 15）**：身份与边界已由 `ops-console/README.md` 明确——edge 本地维护控制台（维护工程师用，可重启服务/看日志/调自愈参数，走 edge-api 管理员 API Key），区别于现场只读大屏（`Drycontroller-frontend`/bigscreen）和云端 cloud-console（多节点/租户/license/OTA/job）。代码已存在但未接 compose，落地排期靠后（见 §9 Phase 6）。形态对比：
- **edge-api StaticFiles 挂卷托管（推荐）**：`npm run build` 产物（`ops-console/dist`）经宿主机目录只读挂进 edge-api，挂载在 `/console` 子路径，与 `/api/v1` 同源 → 零 CORS、WebSocket 同源；不新增容器，容器数 **15**；前端更新只替换挂卷产物，不重建 edge-api 镜像。
- **独立 nginx 容器**：单独静态服务容器 → 新增 1 容器（**16**），且需反代 `/api` 或开 CORS；前后端发布解耦在单人开发下无实际收益，不选。

> 注：基础层（§3）无 web server，"基础层 nginx 托管不新增容器"不成立——用 nginx 必然 +1；只有 edge-api 托管才能保持 15。

---

## 8. 安全与证据边界

沿用Edge身份/License、鉴权、可信命令、白名单、限幅、质量失效锁写与审计。PLC保留最终设备保护，控制入口简化不替代硬接线急停或人员安全认证。

旧多入口协议具体寄存器不再是新方案的整包验收条件；必要安全语义必须在替代接口中得到验证。未部署的新设计不能用来绕过现役authority/epoch门禁，也不能用模拟器PASS替代现场验收。

## 9. 后续实施顺序

1. 取得专用屏型号/工程与当前PLC梯形图，核对屏→Edge协议、M/D赋值和保护路径。
2. 按[I/O 点表](../plc/io-map.html)选用/弃用清单核定Edge在线主路径和HMI2离线兜底路径的接口、地址、类型、倍率、范围和生效条件。
3. Edge主控、HMI2直连兜底、停止、过期/重复请求、断线/重启、结果未知、应急回切及保护覆盖先隔离测试，再按批准窗口逐点现场验收。
4. 仅把验收通过的点加入生产白名单；自动调节单独验收。专用屏必要功能迁至HMI2后，且HMI2在线主路径和离线兜底都验收后，方可撤屏。

本轮只修订设计和证据，运行代码/配置、现场写入和部署均未改变。

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

以下为新控制架构的后续验收要求，不代表本轮已测试或部署；具体失联动作/超时须先签收。

| 场景 | 预期 |
|---|---|
| Edge业务停止/DAQ退出/命令活跃信号失效 | Edge停止新写；HMI2可在签收条件下进入直连PLC应急模式；PLC拒绝过期Edge请求并执行已签收失联动作，不默认D5291或hold-last |
| MQTT或签名链不可用 | 停止新command，AUTO_PAUSED或保持MANUAL，错误可见/可审计 |
| InfluxDB不可用 | 采集降级，控制所需数据缺失则暂停，不以旧值冒充新鲜 |
| edge-api不可用 | Edge无新command；HMI2直连兜底只有在排他执行许可、本地授权、持久日志和未知结果锁等签收条件成立时可写，否则只读；PLC保护和必要本地停机独立有效 |
| Modbus失败/回执丢失 | 区分明确拒绝与结果未知；查状态，不重放危险写入 |
| 专用屏试图绕过Edge | 生产直写被拒绝；专用屏仍走Edge代理 |
| HMI2在Edge在线时试图直连PLC | 拒绝并提示主路径可用；只有Edge不可用且应急条件满足时才允许直连 |
| 多屏/算法同时操作 | Edge统一授权并拒绝冲突；PLC不需要知道哪个屏幕发送 |
| 过期/重复/旧会话请求 | Edge/PLC按最小接口拒绝，不执行旧启动或调频 |
| 越界/单次或累计变化率超限 | Edge校验，DAQ拒绝非法值，PLC最终限幅/保护不被旁路 |
| 任一授权屏幕人工调频 | Edge立即MANUAL、停止AUTO生成、冻结/丢弃未发AUTO；已在途结果查清后才发人工命令 |
| PLC本地/检修限制生效 | Edge停发，PLC独立拒绝不允许的请求，状态可读 |
| 维护锁期间网络恢复 | 锁在PLC侧保持，仅人工解除；不能自动启动/接管 |
| Edge/PLC重启或正常模式切换 | 旧请求不重放、AUTO重新授权、从有效目标无扰衔接，保护动作优先 |
| 输出为1但设备无运行反馈 | 显示输出与真实反馈的区别，不标设备已运行 |

---

## 12. 需同步的文档

v2 被采纳后须持续同步以下文档，避免团队按旧链路继续实现：

- `docs/ops/remote-debug-dev.md`：已改为宿主机 Tailscale / Headscale 运维说明。
- `docs/architecture/data-flow.md`：已同步为 suggestion -> edge-api signed command -> edge-daq。
- `docs/architecture/control-strategy.md`：已同步为 suggestion / command 双协议。
- `docs/architecture/system-overview.md`：已同步 edge-daq command 消费、ai-ctrl-optimizer 不直写 PLC 与 edge-api 进程内 License admission。
- `docs/ops/后端参数配置说明.md`：独立授权容器参数已删除；Tailscale 与控制 topic/ACK 参数以当前运行合同为准。
