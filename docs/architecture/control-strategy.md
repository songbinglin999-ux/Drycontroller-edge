# 🎛 控制策略说明（Control Strategy）

> 版本：v1.7（2026-06-29 EDGE-V2-P3 简化 AUTO 状态机）
> 关联模块：`edge-api/control_core`、`edge-daq`、`hmi-runtime`、PLC 控制逻辑
> 关联文档：`system-overview.md`、`data-flow.md`、`../plc/io-and-control-contract.md`

---

> **当前验收边界**：当前生产只开放 edge-api 侧六个变频频率目标写入，替代人工设定目标频率；DryHMI PLC direct 已确认为最终必做能力，但未通过真实 PLC/HMI e2e 前生产保持 disabled。生产操作不是逐条点确认；操作员只选择 `MANUAL` 或 `AUTO`，`AUTO` 授权后由 edge 自动签发六频率目标，任一 gate 失败进入 `AUTO_PAUSED`（自动暂停（需重新授权））/ 退回手动。PLC 仍负责现场顺控、料位报警后的停/延时/恢复、联锁、设备保护和实际输出。完整工艺联锁、PLC authority、DryHMI direct、稳态优化统一进入最终 e2e，不再按 V1/V2/V3 分期验收。

## 0. 真实控制问题定义

DryController 的最终控制目标是 **最终成品出料湿度 `MT1_H` 稳定在工艺目标区间**。这不是“多级联锁防堵”问题，也不是“某个电机过流就降一点末段给料”的单变量规则问题。堵料、过流、料位和设备保护是约束层 / 保护层；它们可以挂起控制或限制动作，但不能替代湿度闭环目标。

真实控制对象是一条有强滞后、多变量耦合、持续扰动的整线动态系统：

- 主要扰动：进料湿度持续变化、人工/设备给料波动、一级/二级烘干主机负载变化、锅炉供热波动、下游滚筛/出料负载变化。
- 主要可调量：三路上料频率 `VFM1/2/3_FREQ_SP` 和三路排湿风机频率 `VF1/2/3_FREQ_SP`。
- 关键过程量：最终出料湿度 `MT1_H`，预/一级/二级排湿风机处蒸汽温度 `FTT1_T/FTT2_T/FTT3_T`，锅炉炉口温度 `TT1_T/TT2_T`，未来一级/二级出口温度，主机负载电流（例如 `M5_I`、`M10_I`），以及下游负载/料位状态。
- 风机不是普通冷却风机，而是从烘干机内抽走蒸汽的排湿风机；风机变大会改变排湿、热量抽吸和锅炉侧火/热进入烘干机的平衡，不能简单按“湿度高就加风”处理。
- `AUTO` 必须按慢周期、小步、限幅、冷却、等待响应和可挂起的方式运行；任何策略都必须能解释“为什么调、调哪一路、等多久看响应、什么条件下停止继续调”。

因此，后续 `control_core` / `ai-ctrl-optimizer` 的关闭条件不能写成“多级防堵规则已完成”。可接受的关闭口径必须至少证明：控制逻辑以 `MT1_H` 为主目标，能区分给料偏重、供热不足、排湿不足、风机抽火/热平衡风险、主机负载异常和下游不畅，并在数据不足或响应不符合预期时进入 `AUTO_PAUSED`。

### 0.1 变频器频率必须拆分 SP / MV / PV

六路变频频率不能只暴露一个旧程序命令值。一个值既不能同时作为 edge 写入目标，也不能同时代表 PLC 内部目标、PLC 输出命令和变频器实际反馈。当前 SamSoar CSV 只证明 `D30-D35` 最终 `MOV` 到 `AO140-AO151`，没有证明 PLC 与变频器通讯或读取实际频率/状态；这条旧链路不进入 DryController 采集/控制路径。若 PLC 不暴露 D5100 写入 SP、PLC 有效目标、PLC 输出命令、PV/status invalid/not-wired 标记和保护覆盖结果，则 edge 无法判断 PLC 是否接受目标、斜坡后输出命令是多少、变频器是否被本地/保护覆盖，也无法做 ACK/读回闭环。

最终统一接口应拆清以下语义，最终位号可由 PLC 点表命名，但语义不能合并：

| 语义 | 方向 | 用途 |
|---|---|---|
| Edge/HMI 目标频率 SP | edge/HMI -> PLC | edge 在线通过 `D5100-D5299` 单路事务写入，`D5104` 为通道、`D5110` 为单个频率值；edge 不可用且 PLC 授权时 DryHMI 通过 `D5550-D5599` direct 事务写 DryHMI 目标缓冲区；表示上位机希望 PLC 执行的目标 |
| PLC 接受后的有效目标 | PLC -> edge | PLC 经模式、联锁、限幅、斜坡策略接受后的内部目标；只能用于解释 PLC 当前追哪个目标 |
| PLC 输出给变频器的命令 MV | PLC -> edge | PLC 实际下发给变频器的输出命令，反映限幅/斜坡后的执行量 |
| 变频器实际反馈 PV | VFD -> PLC -> edge | 当前 invalid/not-wired；后续新增反馈线或通讯后另签，用于判断执行是否到位 |
| VFD 运行/故障/本地覆盖状态 | VFD/PLC -> edge | 当前 invalid/not-wired；后续新增反馈线、通讯或硬接点后另签，用于判断变频器是否远程可控、是否故障、是否被本地或保护逻辑覆盖 |

`D30-D35` 属于 PLC 原工程/PLC 屏侧旧频率链路，不能标为 `*_FREQ` 实际反馈，不能作为写入 SP 地址，也不再作为 DryController 读回/对账输入。没有 D5100 目标 SP、PLC 有效目标/MV、PV/status invalid/not-wired 标记、保护覆盖结果和报警码之前，六频率 `AUTO` 只能停留在 shadow / 诊断建议，不能作为现场控制验收。真实 VFD feedback 和 VFD 状态需在反馈线或通讯补齐后另行签收。

## 1. 控制目标与边界

### 1.1 控制目标

- 在满足 **安全与工艺约束** 的前提下：
  - 使成品含水率稳定在工艺目标区间内
  - 尽量降低能耗与生产波动
  - 提升生产线自动化与稳定性

### 1.2 控制边界

- **当前生产阶段**：edge/HMI 只通过 edge-api 写六个变频频率目标。`MANUAL` 下人工设目标频率，`AUTO` 下 edge 规则/算法自动签发目标频率；两种模式都必须经过模式授权、软点白名单、范围校验、变化率/冷却限制、ACK/NAK、读回确认和审计。PLC 执行现场逻辑、安全、顺控、联锁、设备保护和最终输出。
- **最终必做能力**：edge 不可用且 PLC 授权时，DryHMI 通过 PLC direct 写 DryHMI 目标缓冲区；该路径必须校验 owner/epoch/direct-enable、ACK/NAK、读回和审计，不能绕过 PLC 输出提交。
- **后续监督控制/稳态控制**：edge 根据电流、料位事件、温度、湿度、下游能力等生成 6 个频率策略；PLC/VFD/硬接线保留最终物理安全兜底。

---

## 2. 控制对象与可调参数

V1 只允许以下六个频率目标软点：

- `VFM1_FREQ_SP`
- `VFM2_FREQ_SP`
- `VFM3_FREQ_SP`
- `VF1_FREQ_SP`
- `VF2_FREQ_SP`
- `VF3_FREQ_SP`

链速、启停命令、风门、温度设定、锅炉/燃烧控制等不属于当前六频率验收范围。DryHMI 直连和 PLC 控制权锁属于最终必做能力，但生产启用前必须完成真实 PLC/HMI e2e。实际 Tag 名与含义请参照：`docs/plc/io-and-control-contract.md`。

---

## 3. 控制模式

### 3.1 手动模式（MANUAL）

- V1 操作员通过 HMI/edge-api 修改六个频率目标软点，PLC 继续执行现场工艺逻辑和安全保护。
- 系统仍做白名单、范围、变化率、ACK/NAK、读回和审计；手动不是绕过安全闸门。
- 非六频率目标的 setpoint、启停和联锁动作仍由 PLC/现场系统负责。
- 适用场景：
  - 初次投产 / 试车
  - 自动模式尚未充分验证
  - 故障或异常情况下的紧急接管

### 3.2 自动模式（AUTO）

- 生产流程：

```text
操作员选择 AUTO
   ↓
edge/control_core 基于实时数据、历史窗口、softsensor 证据计算六个频率目标
   ↓
control_orchestrator 做 freshness / mode / whitelist / range / rate / cooldown / ACK-NAK / readback gate
   ↓
edge-api 自动签发 signed command
   ↓
edge-daq 写 PLC 六个 SP，记录 ACK/NAK 与读回结果
   ↓
任一 gate 失败 → AUTO_PAUSED，停止自动下发并展示原因
```

- 特点：
  - 操作员不需要坐在控制屏前逐条点确认；人工决策点是进入/退出 `AUTO`。
  - V1 控制目标可以由规则/算法生成，但执行面只允许六个频率目标软点。
  - 每次自动动作必须可解释、可回放；大屏/HMI 展示当前目标、算法理由、最近动作、挂起原因和审计记录。
  - `AUTO` 失败时不降级为盲目继续写，而是进入 `AUTO_PAUSED` 或转 `MANUAL`。

### 3.3 自动暂停（AUTO_PAUSED）

`AUTO_PAUSED` 中文显示为“自动暂停（需重新授权）”。它不是操作员可选模式，也不是停机/故障；它表示 AUTO 被 gate 失败、PLC 本地接管、手动调频、NAK/timeout、edge 恢复等事件打断。条件恢复后不能自动回 AUTO，必须人工重新授权。

- 触发：
  - 实时数据 stale / 缺关键 tag。
  - 目标值越界、变化率/冷却限制不满足。
  - PLC NAK、写入失败或读回不一致。
  - 维护锁、急停、本地接管、edge 控制不健康。
- 行为：
  - 停止新的自动 command。
  - 保留诊断、建议和可解释 evidence。
  - HMI 只提供“转手动 / 重新授权 AUTO / 查看原因”，不要求逐条补确认。

### 3.4 监督控制扩展

- 未来可选模式：
  - edge-api 在 `AUTO` 授权层级下，把 suggestion 转成 signed command
  - `edge-daq` 只消费 signed command 写 PLC，AI 不直接写 PLC
  - edge-api 仍必须执行 range guard、ramp/slew limiter、工艺联锁、签名和审计
  - PLC/VFD/硬接线保留最终物理安全兜底
- 当前生产不开放 HMI 直连 PLC；edge-api 授权 AUTO 前必须看到实时数据 fresh、`AUTHORITY_OWNER=EDGE`、`AUTHORITY_EPOCH>0`、`AUTH_TRANSITION_STATE=0`、`HMI_DIRECT_ENABLE=false`，且 MQTT 与命令签名配置可用。edge-api 不在 AUTO dispatch 中自动申请/抢回 owner。
- 启用前必须：
  - 在现场充分测试
  - 与工艺与安全部门共同评估后决策

---

## 4. 控制安全边界与限幅策略

所有控制策略都需满足以下约束（配置集中在 `configs/limits.yaml`）：

- **物理上下限约束**
  - V1 六个频率目标必须在现场签收范围内（默认文档占位为 0-50Hz，最终以 PLC/变频器参数签收为准）
- **变化率约束**
  - 每次调整的幅度不超过 `MAX_DELTA`
  - 一定时间窗口内的累计变化不超过 `MAX_ACCUM_DELTA`
- **工艺联锁约束**
  - V1 只做六频率目标写入许可与降载建议；料位报警后的停转/延时开启等现场工艺逻辑仍在 PLC
  - V2/V3 才把更多工艺联锁纳入 edge 监督控制状态机

V1 下发的六频率目标必须先经过 edge/HMI 侧模式授权、白名单、范围、变化率、冷却、ACK/NAK 和读回 gate，再交给 PLC 执行。PLC 仍承担现场工艺逻辑、安全联锁、设备保护和最终输出。

---

## 5. V2/V3 ai-ctrl-optimizer 策略逻辑（概述）

> 本节描述后续监督控制/稳态控制方向，不作为 V1 六频率目标写入验收条件。V1 可以只用固定规则或简单启发式生成 AUTO 目标值；复杂优化库不是 V1 前置条件。

### 5.1 输入

- 来自 MQTT / InfluxDB 的实时工艺数据：
  - 当前各段温度、尾气温度、料温、链速、风机频率等
- 来自 `ai-softsensor` 的含水率估计与置信度
- 来自配置库（PostgreSQL/`limits.yaml`）的：
  - 目标含水率区间
  - 上下限与变化率约束
  - 不同原料/工况的策略参数（如权重、目标偏好）

### 5.2 输出

- 建议六个变频频率目标 setpoint
- 可选：执行优先级与建议执行时机（立即/缓慢调整）

### 5.3 料流/给料控制逻辑（V2/V3）

- 当含水率偏高或出口料温偏高时：
  - 优先降低相关给料频率，减少进入异常段的物料量
  - 降低幅度受 `MAX_DELTA_FREQ` 与工艺约束限制
- 当含水率偏低且品质允许时：
  - 适当提高给料频率，在不牺牲品质的前提下降低能耗、提升产能
- 在风量/热量不足或安全风险存在时（如温度过低/过高）：
  - 暂停提频或适当降频，优先保障安全与稳定

### 5.4 风机控制逻辑（V2/V3）

- 当含水率偏高、尾气温度偏高时：
  - 适度提升抽风频率，提高排湿能力
- 当含水率接近下限或温度偏低时：
  - 避免过高抽风导致能耗上升与工艺过干
- 抽风频率调整同样受 `MAX_DELTA_FREQ` 与安全约束控制

### 5.5 温度控制边界

- AI 与控制策略 **不直接调节炉膛火力**，但会在以下场景中尊重温控边界：
  - 当任一关键温度 Tag 接近 `TEMP_MAX_SAFE` 时，禁止增加链速和风量
  - 当温度持续低于工艺下限时，提示人工检查炉膛/燃烧系统
- 具体温控上下限与报警逻辑在 PLC 程序和 `limits.yaml` 中共同定义。

### 5.6 预烘干热耦合逻辑

- 预烘干段利用尾气/余热，其工况变化会影响后续主烘干段：
  - 尾气温度、预烘干出口料温作为特征输入参与链速与抽风决策
  - 当预烘干能力不足（出口料温偏低）时，控制策略会更保守地提升链速
- 未来可进一步在 `ai-ctrl-optimizer` 中显式建模“预烘干 ↔ 主烘干”的能量耦合关系。

### 5.7 综合策略示例

- 规则 + 优化混合方式：
  - 当含水率偏高 → 优先降低链速或提高风量
  - 当含水率偏低 → 适当提高链速以提升产能
  - 同时考虑炉温、预烘干能力等限制，避免过度调整

---

## 6. 控制链路实现要点

### 6.1 控制建议发布

- Topic 示意：`control/suggestion/{line}`
- Payload 示例：

```json
{
  "line": "L1",
  "timestamp": "2025-01-01T12:00:00Z",
  "suggestions": [
    { "tag": "VFM2_FREQ_SP", "value": 18.0 },
    { "tag": "VF2_FREQ_SP", "value": 35.0 }
  ],
  "reason": "moisture_high",
  "ai_confidence": 0.92
}
```

### 6.2 控制执行（/api/v1/control/recommendations/*）

- `edge-api` 接口负责：
  - `GET /api/v1/control/status` 返回当前 `MANUAL` / `AUTO` / `AUTO_PAUSED` 产品态、暂停原因、重授权要求、最近 authority snapshot 和最近 command/ACK 结果
  - `POST /api/v1/control/mode` 人工切 `MANUAL` 或授权 `AUTO`；`AUTO_PAUSED` 不是可选 mode
  - `POST /api/v1/control/auto/dispatch` 执行一次 AUTO tick：选取一个 actionable 六频率 action，经 `ControlCommandService.send_set(..., mode=2, wait_ack=True)` 下发
  - 校验六软点与目标值的合法性（模式授权、白名单、范围、最小变化率/冷却、ACK/NAK、读回、API key、审计）
  - 记录操作日志（操作者、理由、前后值）
  - 将合规命令转换为 MQTT 消息，交给 `edge-daq` 写入 PLC
  - `GET /recommendations` 只展示 advisory；不提供逐条确认下发入口。`/manual-confirm` 仅是 MANUAL 人工调频入口，AUTO 写 PLC 的唯一入口是 `/auto/dispatch`

### 6.3 AUTO 控制建议如何平滑下发

- **防抖**：仅当连续多次建议方向一致、且偏差超过阈值时才生成控制建议
- **缓变**：对比当前 setpoint 与 AI 建议值，按最大步长/变化率缓慢逼近，而非一次性跳变
- **节流**：限制单位时间内的调整次数，避免“抖动式”频繁调节
- **可见性**：大屏上同时展示当前 setpoint 与 AI 目标值，方便操作员理解和判断

---

## 7. 降级与回退策略

当发生以下情况时，控制策略需要自动或人工降级：

- 规则/AI 推理异常或置信度持续过低 → 进入 `AUTO_PAUSED`，停止自动下发，仅展示诊断或转手动
- 通讯异常（PLC / MQTT） → 暂停下发新 setpoint，保持当前稳态
- 发现控制策略导致工艺波动增大 → 人工切回纯手动模式，并通知算法/工艺团队

相关降级与自愈流程详见：`self-healing.md` 与 `risk-assessment.md`。

---

## 8. 现场应用建议

- 在项目早期：
  - 先启用 `MANUAL` + shadow 诊断，积累经验与数据
  - 再启用 `AUTO` 小步长、低频率、可挂起的六频率自动设定
  - 将 AUTO 建议/动作与实际人工操作进行对比评估
- 在策略成熟后：
  - 可在部分时段/部分工况下试点半自动控制
  - 针对每种物料类型和工况建立相应的策略参数配置
