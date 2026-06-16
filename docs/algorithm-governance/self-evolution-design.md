# 自进化与自适应参数治理设计

> 状态：v0.1 草案（2026-06-08 CST）
> 范围：算法诊断阈值、长期偏移基线、模型候选更新的受控治理
> 不包含：PLC 安全阈值、控制写入权限、自动闭环控制
> 关联文档：`docs/control-core/e-line-diagnosis-rules.md`、`docs/process-intelligence/long-term-drift-monitoring.md`

---

## 0. 结论

这里的“自进化”不是让系统自己随便改控制逻辑。DryController 当前阶段只允许做**受控自标定 / 自适应参数治理**：

```text
运行数据 -> 候选参数 -> 历史回放验证 -> 在线 shadow 对比 -> 人工确认 -> 版本化生效 -> 可回滚
```

自进化可以改善诊断阈值、长期漂移基线和模型候选版本，但不能越过安全边界，不能自动把建议变成控制命令。

---

## 1. 适用范围

| 对象 | 可自适应内容 | 不可自适应内容 |
|---|---|---|
| E 线 `control-core` | 电流趋势阈值、持续时间、置信度权重、设备运行基线、低/高负载判定参数 | `actionable=false`、控制权限、PLC 写入、P0 gate |
| G 线 `process-intelligence` | 稳定运行基线、漂移评分、相近工况聚类、长期窗口阈值 | 制造结论自动定责、PLC 安全报警值 |
| softsensor | 重训触发、候选模型评估、模型漂移提示 | 自动替换生产模型、绕过化验标签验证 |
| edge-api 控制链 | 不适用 | range guard、签名、ACK/NAK、mode gate、急停、审计 |

允许自适应的是“判断参数”；不允许自适应的是“安全边界”和“控制权限”。

---

## 2. 硬边界

这些内容绝不能被自进化流程自动修改：

- PLC 安全阈值、联锁、急停、watchdog、上电默认、手自动模式。
- edge-api 控制写入 range guard、HMAC 签名、ACK/NAK、mode gate。
- `P0-SAFETY`、`P0-CTRL-NAK`、`P0-CTRL-MODE` 的阻断状态。
- `actionable=false` / `control_actionable=false`。
- 52 位号身份、`configs/tags.yaml` 点位语义、`modbus-address-map.yaml` 地址映射。
- HMI/edge-api 控制软点清单和设定值上下限；V1 固定为六个频率目标，V2/V3 才允许扩展。
- 已确认异常、停机、维护、传感器 stale 期间的数据，不得自动吸收到健康基线。

如果某个候选更新需要改变这些内容，系统必须拒绝生成自动候选，只能转为人工设计变更。

---

## 3. 参数分层

### 3.1 PLC 安全参数

来源：PLC / 集成商 / 安全验收。

用途：保护人、设备和产线。

治理：只走 PLC 安全设计与验收流程。算法只能引用这些报警作为 evidence，不能改。

### 3.2 算法诊断参数

来源：edge 历史数据、规则设计、现场标定。

例子：
- `I_M*` 高负载 / 低负载的相对基线阈值。
- 30 秒电流突升判定。
- 异常持续多久从 L0 观察升到 L1 提示。
- 有上下游佐证时置信度如何提升。

治理：允许自适应生成候选，但必须 shadow 对比和人工确认。

### 3.3 工艺长期基线

来源：稳定运行窗口。

例子：
- 每台设备 7/14/30 天正常运行电流中位数。
- 热效率关系基线，如 `TT2 -> FTT3` 响应。
- `MT1`、softsensor、离线化验之间的长期偏差。

治理：允许周期性生成候选基线，不能自动把异常窗口学成正常。

### 3.4 模型候选版本

来源：离线化验标签、特征历史、模型训练流程。

例子：
- softsensor 重训候选。
- 特征时延重校准。
- 模型漂移检测。

治理：只能生成候选和评估报告；生产模型替换需要人工批准。

---

## 4. 数据准入

自进化只能使用合格窗口。

| 状态 | 是否可用于候选训练 | 说明 |
|---|---|---|
| `stable_run` | 是 | 设备运行、数据 fresh、无高置信异常 |
| `startup` | 否 | 启动阶段电流/温度不代表稳态 |
| `shutdown` | 否 | 停机阶段不代表稳态 |
| `stale` | 否 | 数据过期或窗口不完整 |
| `realtime_anomaly` | 否 | E 线高置信异常窗口 |
| `plc_alarm` | 否 | PLC 已报警窗口 |
| `maintenance` | 否 | 检修、点动、调试、换件 |
| `unknown_context` | 限制使用 | 只可参与低置信候选，不可直接提升 |

稳定窗口过滤是自进化第一道闸。没有它，自学习会把堵料、传感器坏点、停机波动学成“正常”。

---

## 5. 生命周期

### 5.1 冷启动

现场数据刚接入时：

- 前 7 天只采集，不自动更新。
- 只生成观察级统计，不生成高置信候选。
- 每台设备单独建初始画像，不共用统一阈值。
- 缺少额定电流 / 料位极性 / 量程时，候选只能标记为 `weak_candidate`。

### 5.2 候选生成

周期：
- 诊断阈值：默认每日或每周生成候选。
- 长期基线：默认每日滚动更新候选，周级确认。
- 模型候选：只在新增足够化验标签或检测到模型漂移时生成。

方法：
- 使用中位数、MAD、IQR 等稳健统计。
- 按设备、运行状态、相近工况分组。
- 候选相对上一版本变化需要限幅，避免一天数据把参数推偏。
- 异常窗口、维护窗口、停机窗口必须排除。

### 5.3 历史回放验证

候选参数先跑历史数据回放：

| 指标 | 意义 |
|---|---|
| alert_rate | 提示频率是否明显失控 |
| false_positive_rate | 误报是否增加 |
| missed_alarm_proxy | 是否漏掉后续 PLC 报警或人工确认异常 |
| lead_time | 对异常是否更早发现 |
| abstain_rate | 数据不足时是否正确放弃判断 |
| stability | 相邻版本参数是否稳定 |

没有人工标签时，先用 PLC 报警、control-core 高置信诊断、人工处理记录作为弱标签。

### 5.4 在线 shadow

候选参数不直接生效，先与生产参数并行跑：

```text
production parameters -> 真实输出给前端
candidate parameters  -> shadow 输出，只记录不展示或仅工程师可见
```

shadow 至少覆盖：
- 正常稳定运行。
- 一次启动/停机。
- 一批原料或一个生产周期。
- 若现场允许，至少 3-7 天。

### 5.5 提升与生效

候选提升条件：

- 历史回放指标不差于当前版本。
- shadow 期间提示频率可接受。
- 没有触碰硬边界。
- 人工确认候选报告。
- 有明确回滚版本。

生效后必须记录版本和审计事件。

### 5.6 回滚

任何候选生效后都必须可回滚：

- 误报率异常升高。
- 操作员反馈提示干扰生产。
- PLC 报警或人工确认异常被漏掉。
- 传感器更换、量程修正、工艺大改导致旧基线失效。

回滚不需要等待下一轮训练；只要切回上一版本参数即可。

---

## 6. 版本产物

建议按类型分开存版本产物，避免实时诊断和长期制造基线混在一起。

```text
configs/algorithm-baselines/
  e-line/
    diagnosis-thresholds-v20260608.yaml
    process-baseline-v20260608.yaml
    model-candidate-report-v20260608.json
```

诊断阈值版本内容：

```yaml
schema: drycontroller.diagnosis_thresholds.v1
line_id: default
version: diagnosis-thresholds-v20260608
source_window:
  start: "2026-06-01T00:00:00Z"
  end: "2026-06-08T00:00:00Z"
excluded_windows:
  - reason: plc_alarm
    count: 12
parameters:
  I_M12:
    high_load:
      baseline: 18.4
      spread: 1.2
      warning_multiplier: 3.0
      min_duration_s: 30
promotion:
  mode: shadow
  previous_version: diagnosis-thresholds-v20260601
```

长期基线版本内容：

```yaml
schema: drycontroller.process_baseline.v1
line_id: default
version: process-baseline-v20260608
assets:
  M12:
    metric: stable_run_current_median
    baseline_7d: 18.4
    baseline_30d: 16.1
    spread_mad: 1.2
    sample_count: 8421
```

---

## 7. 输出契约

候选参数报告：

```json
{
  "schema": "drycontroller.algorithm_candidate.v1",
  "line_id": "default",
  "candidate_id": "diagnosis-thresholds-v20260608",
  "scope": "control-core",
  "status": "shadow",
  "source_window": {
    "start": "2026-06-01T00:00:00Z",
    "end": "2026-06-08T00:00:00Z"
  },
  "changes": [
    {
      "asset": "I_M12",
      "parameter": "high_load.baseline",
      "from": 16.1,
      "to": 18.4,
      "change_percent": 14.3
    }
  ],
  "validation": {
    "backtest_alert_rate_delta": -0.04,
    "missed_alarm_proxy_delta": 0.0,
    "shadow_days": 3
  },
  "promotion": {
    "requires_human_approval": true,
    "rollback_to": "diagnosis-thresholds-v20260601"
  }
}
```

运行时事件：

```json
{
  "schema": "drycontroller.algorithm_governance_event.v1",
  "event": "candidate_promoted",
  "candidate_id": "process-baseline-v20260608",
  "approved_by": "operator_or_engineer",
  "rollback_to": "process-baseline-v20260601"
}
```

---

## 8. 人机协作

自进化不应该让操作员面对一堆参数。UI 只需要给工程师看三类信息：

- 候选更新改了什么。
- 为什么认为它更好。
- 是否允许生效，以及如何回滚。

操作员侧只看到业务结果：提示变准了、误报变少了、长期制造风险有解释。参数审批应放在工程师 / 维护角色，不放在普通大屏。

---

## 9. 实现顺序

| 顺序 | 内容 | 验收 |
|---|---|---|
| 1 | 稳定窗口过滤器 | 故障/停机/stale/维护窗口不会进入候选训练 |
| 2 | 诊断阈值候选生成 | 覆盖 `I_M*` 高/低负载、30s 突升、持续时间 |
| 3 | 历史回放验证 | 候选与当前版本可对比 alert_rate / missed_alarm_proxy |
| 4 | shadow 运行 | 候选只记录，不影响生产输出 |
| 5 | 版本产物与回滚 | 参数有版本、审计、回滚目标 |
| 6 | 长期基线候选 | 接入 G 线 drift baseline |
| 7 | 模型候选报告 | 接入 softsensor 重训候选，但不自动替换生产模型 |

---

## 10. 开放问题

- 现场是否能提供维护窗口、换件记录、班次和配方；没有这些，候选只能更保守。
- PLC 报警码清单何时确认；它是弱标签质量的关键输入。
- 是否允许工程师在 HMI 或 ops-console 中审批候选参数。
- 参数版本最终落本地文件、数据库，还是两者都落；v0.1 先按文件产物设计。
