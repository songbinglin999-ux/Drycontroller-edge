# PLC 通讯协议（给 PLC 工程师）

只保留 PLC 侧需要做的事情，其他上位链路细节已去掉。

> 2026-06-23 边界：当前 SamSoar CSV 与现场线索只证明 PLC 把六路命令值 `D30-D35` 输出到 `AO140-AO151`，没有证明 PLC 与变频器通讯或读取实际频率。V1 只要求 PLC 提供独立六个频率目标 SP、PLC 有效目标、PLC 输出命令/MV、PV/status invalid/not-wired 标记、状态/安全覆盖和报警码，用于 HMI/edge-api 替代人工设定频率；PLC 继续负责现场逻辑、安全、顺控、联锁、设备保护和实际输出。真实 VFD 实际频率/状态要等反馈线或通讯补齐并签收后再作为 V2/V3 监督控制验收项。

## 1. 通讯参数
- 协议：**Modbus RTU over TCP**（RTU 帧 + TCP 传输，带 CRC；**非**标准 Modbus TCP/MBAP）。2026-06-20 现场实测确认。
- 站号：默认 1
- 端口：**2022**（现场实测；非标准 502）
- 功能码：读 3（保持寄存器，D 字读点）、读 1（线圈，料位/限位等位元件）、读 4（输入寄存器）；写 6/16（控制写，本期按只读决策锁停、未启用）
- 字节序：默认 Big Endian；32 位值字序需按具体点位签收；当前 SamSoar CSV 只证明湿度 `D45` 为 WORD

## 2. 点表字段（请按表格提供）
- 必填：`name`（唯一）、`address`、`function_code`（3/4）、`type`（float/int/int32/uint32/bool）
- 可选：`byte_order`(big/little)、`scale`（工程值 = 原值 * scale）、`unit`、`range[min,max]`、`alarm_low`、`alarm_high`
- 只读 I/O 点：对应 `io-map V2` 52 位号（DI6/AI24/DO16/AO6），edge 只读不写
- V1 控制写入点：只列六个频率目标 SP，不混入 52 个只读 I/O；请标明 `point_role=control_softpoint`
- V2/V3 同步与仲裁点：参数同步、报警同步、状态同步、心跳、HMI/edge 控制权锁、直连启用、无扰切换状态，请标明 `point_role=sync|alarm|status|heartbeat|authority`
- 唯一性：同一功能码下 `(address, function_code)` 不可重复

表头示例（含中文说明）：

| 字段            | 含义              | 示例        |
|-----------------|-------------------|------------|
| name            | 标签名（唯一）    | TEMP.PV    |
| address         | 寄存器地址        | 40010      |
| function_code   | 功能码 3/4        | 3          |
| type            | 数据类型          | float      |
| byte_order      | 字节序 big/little | big        |
| scale           | 缩放（工程=原值*scale） | 1.0  |
| unit            | 工程单位          | °C         |
| range_min       | 最小值            |            |
| range_max       | 最大值            |            |
| alarm_low       | 低报              |            |
| alarm_high      | 高报              |            |
| point_role      | 点角色            | readonly_io/control_softpoint/sync/alarm/status/heartbeat/authority |

## 3. 读取规则（PLC 侧无需改逻辑，只需点表正确）
- `float`：占 2 寄存器，按 `byte_order` 组字
- `int32`/`uint32`：占 2 寄存器
- `int`/`bool`：占 1 寄存器（bool 非 0 即 True）
- 我们会自动合并连续地址批量读取（同功能码、同字节序、同寄存器长度）。

## 4. 写入规则（控制软点）
- 仅 `point_role=control_softpoint` 的 PLC 软点会被写；52 个 I/O 位号全部只读
- 寄存器占用：`float`/`int32`/`uint32` 用 2 个寄存器；`int`/`bool` 用 1 个寄存器（bool 写 0/1）
- 如配置 `scale`：寄存器值 = 工程值 / scale
- 如配置 `range`：超出范围将被拒绝下发
- V1 至少要求写入后可读 PLC 有效目标、PLC 输出命令/MV、PV/status invalid/not-wired 标记、PLC 安全覆盖/报警状态；V2/V3 在反馈线或通讯补齐后再要求真实 VFD 实际频率/状态，并要求 PLC 对每条写入返回 ACK/NAK 与原因码，不满足控制权、模式、范围、联锁或同步条件时必须 NAK，不能静默忽略

## 5. 通讯心跳（V0 已用；V2/V3 可扩展控制门控心跳）
- 必须提供读心跳寄存器（如 50000）和/或写心跳寄存器（如 50001），最终形式联调时确认
- 读：PLC 侧可 0/1 翻转或更新任意变化值；edge 只判断读值是否变化
- 写：edge 周期写递增计数
- 请在点表注明心跳地址、功能码、超时窗口和心跳丢失后切 PLC 本地控制的状态位

心跳推荐：
- 读功能码：3/4（按 PLC 寄存器类型确认）；写功能码：6（单保持寄存器）
- 周期：5s（可调）
- 判定：连续 2~3 次读值不变或读/写失败，判定通讯异常
- edge 写计数回绕：计数达上限可回到 0（无需特殊处理）

## 6. 同步 / 无扰切换 / 控制权锁（V2/V3）
- 参数同步：PLC 与 edge 对关键设定值、当前生效参数、参数版本/更新时间有一致字段。
- 报警同步：PLC 告警码、告警状态、确认/复位状态需可读；需给出报警码表。
- 状态同步：PLC 模式、本地/远程控制权、软点执行状态、最近 ACK/NAK 原因码需可读。
- 无扰切换：edge 控制权切入/切出时，PLC 需保持当前安全输出或按约定斜坡，不允许突变。
- 控制权锁：PLC 是最终执行仲裁点；edge 无心跳、PLC 本地模式、维护锁、联锁触发、HMI 接管时，PLC 拒绝 edge 软点并返回 NAK 原因。
- HMI 直连通道：HMI 可以监视 edge/PLC 状态并发接管请求，但只有 `AUTHORITY_OWNER=HMI` 或 `PLC_LOCAL/LOCAL_MANUAL` 且 `HMI_DIRECT_ENABLE=1` 时，PLC 才接受 HMI 直连写入。
- epoch/代次：edge 或 HMI 写控制软点时需携带当前 `AUTHORITY_EPOCH`；PLC 拒绝 stale epoch，避免 edge 恢复后用旧状态抢写。本项目现场内网不引入 token/签名租约。

建议至少提供这些 authority 字段（名称可按 PLC 习惯调整，但语义需保留）：

| name | 方向 | type | point_role | 说明 |
|---|---|---|---|---|
| CTRL.MODE | PLC -> edge/HMI | int | authority | 当前模式：EDGE_MASTER/HMI_STANDBY/HMI_LOCAL/PLC_LOCAL/LOCAL_MANUAL/MAINTENANCE_LOCK/FAULT_STOP |
| CTRL.AUTHORITY_OWNER | PLC -> edge/HMI | int | authority | 当前控制权 owner：EDGE/HMI/PLC_LOCAL/MAINTENANCE/FAULT |
| CTRL.AUTHORITY_REQUEST | edge/HMI -> PLC | int | authority | 接管/释放请求 |
| CTRL.AUTHORITY_GRANT | PLC -> edge/HMI | int | authority | PLC 对请求的授予/拒绝结果 |
| CTRL.AUTHORITY_EPOCH | PLC -> edge/HMI | int | authority | 控制权代次，写软点时需匹配 |
| HMI.DIRECT_ENABLE | PLC -> HMI/edge | bool | authority | 是否允许 HMI 备用直连写控制软点 |
| CTRL.TAKEOVER_REASON | PLC -> edge/HMI | int | authority | 最近一次接管原因 |
| CTRL.REJECT_CODE | PLC -> edge/HMI | int | authority | 最近一次拒绝原因 |

点表示例（含中文备注）：

| name       | address | function_code | type  | byte_order | scale | unit | range_min | range_max | alarm_low | alarm_high | point_role | 备注                  |
|------------|---------|---------------|-------|------------|-------|------|-----------|-----------|-----------|------------|------------|-----------------------|
| HB.READ    | 50000   | 3             | int   | big        | 1     |      |           |           |           |            | heartbeat  | 心跳读，PLC 可 0/1 翻转；edge 只判断变化 |
| HB.WRITE   | 50001   | 6             | int   | big        | 1     |      |           |           |           |            | heartbeat  | 心跳写，edge 写递增计数 |
| FTT1       | 40010   | 3             | float | big        | 1.0   | °C   |           |           |           |            | readonly_io | 主机1风温采集         |
| VFM1_FREQ_SP | 41020 | 3             | float | big        | 1.0   | Hz   | 0         | 50        |           |            | control_softpoint | VFM1 目标频率软点 |
| CTRL.AUTHORITY_OWNER | 41030 | 3       | int   | big        | 1     |      |           |           |           |            | authority  | 当前控制权 owner      |
| CTRL.AUTHORITY_EPOCH | 41031 | 3       | int   | big        | 1     |      |           |           |           |            | authority  | 控制权代次            |
| HMI.DIRECT_ENABLE | 41032 | 3          | bool  | big        | 1     |      |           |           |           |            | authority  | HMI 直连写入授权      |
| ALARM.CODE | 42010   | 3             | int   | big        | 1     |      |           |           |           |            | alarm      | 当前 PLC 告警码       |

## 7. 联调检查清单
V1 最小联调：
1) 确认站号、端口、功能码、字节序。
2) 提供完整 52 点只读点表，标明地址、类型、scale、byte_order。
3) 提供六个频率目标 SP 的地址、类型、scale、byte_order、range。
4) 提供 PLC 有效目标、PLC 输出命令/MV、PV/status invalid/not-wired 标记、PLC 安全覆盖状态和报警码。
5) 验证 HMI/edge-api -> edge-daq -> PLC -> 变频器命令/MV 变化；当前不验收 VFD 实际反馈回读，越界/未授权/edge 不健康时拒绝。

V2/V3 监督控制联调：
1) 确认站号、端口、功能码、字节序。
2) 提供完整点表，标明可写点。
3) 提供心跳寄存器与心跳丢失切本地状态。
4) 提供参数同步、报警同步、状态同步、无扰切换、HMI/edge 控制权锁和 HMI 直连启用字段。
5) 联调前用测试 PLC/模拟器自查：无重复地址、功能码正确、字节序与类型匹配，ACK/NAK/原因码可复现。
