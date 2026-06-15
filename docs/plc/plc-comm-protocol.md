# PLC 通讯协议（给 PLC 工程师）

只保留 PLC 侧需要做的事情，其他上位链路细节已去掉。

> 2026-06-15 边界：PLC 负责软点控制层、参数同步、报警同步、状态同步、执行、采集、ACK/NAK/原因码、无扰切换、控制权锁，以及读取 edge 心跳并在心跳丢失时切回 PLC 本地控制。edge 负责工艺控制算法、控制状态机和软件侧安全兜底。现场 HMI 保留备用直连通道，但只有 PLC 授权 `HMI_DIRECT_ENABLE` 后才允许写控制软点。

## 1. 通讯参数
- 协议：Modbus TCP
- 站号：默认 1（若不同请告知）
- 端口：默认 502
- 功能码：读 3（保持寄存器）、读 4（输入寄存器）；写 6/16（单/多寄存器）
- 字节序：默认 Big Endian（如少量点需 Little，请在表中标注）

## 2. 点表字段（请按表格提供）
- 必填：`name`（唯一）、`address`、`function_code`（3/4）、`type`（float/int/int32/uint32/bool）
- 可选：`byte_order`(big/little)、`scale`（工程值 = 原值 * scale）、`unit`、`range[min,max]`、`alarm_low`、`alarm_high`
- 只读 I/O 点：对应 `io-map V2` 52 位号（DI6/AI24/DO16/AO6），edge 只读不写
- 控制写入点：单独列为 PLC 软点 / SP / 模式点，不混入 52 个只读 I/O；请标明 `point_role=control_softpoint`
- 同步与仲裁点：参数同步、报警同步、状态同步、心跳、HMI/edge 控制权锁、直连启用、无扰切换状态，请标明 `point_role=sync|alarm|status|heartbeat|authority`
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
- PLC 对每条写入返回 ACK/NAK 与原因码；不满足控制权、模式、范围、联锁或同步条件时必须 NAK，不能静默忽略

## 5. 心跳（可选）
- 必须提供读心跳寄存器（如 50000）和/或写心跳寄存器（如 50001），最终形式联调时确认
- 读：PLC 侧计数递增；写：我们周期写递增计数
- 请在点表注明心跳地址、功能码、超时窗口和心跳丢失后切 PLC 本地控制的状态位

心跳推荐：
- 功能码：3（保持寄存器）
- 周期：5s（可调）
- 判定：连续 2~3 次读值不变或读/写失败，判定通讯异常
- 计数回绕：计数达上限可回到 0（无需特殊处理）

## 6. 同步 / 无扰切换 / 控制权锁
- 参数同步：PLC 与 edge 对关键设定值、当前生效参数、参数版本/更新时间有一致字段。
- 报警同步：PLC 告警码、告警状态、确认/复位状态需可读；需给出报警码表。
- 状态同步：PLC 模式、本地/远程控制权、软点执行状态、最近 ACK/NAK 原因码需可读。
- 无扰切换：edge 控制权切入/切出时，PLC 需保持当前安全输出或按约定斜坡，不允许突变。
- 控制权锁：PLC 是最终执行仲裁点；edge 无心跳、PLC 本地模式、维护锁、联锁触发、HMI 接管时，PLC 拒绝 edge 软点并返回 NAK 原因。
- HMI 直连通道：HMI 可以监视 edge/PLC 状态并发接管请求，但只有 `AUTHORITY_OWNER=HMI` 或 `PLC_LOCAL/LOCAL_MANUAL` 且 `HMI_DIRECT_ENABLE=1` 时，PLC 才接受 HMI 直连写入。
- token/代次：edge 或 HMI 写控制软点时需携带当前 `AUTHORITY_TOKEN`（或等价代次字段）；PLC 拒绝 stale token，避免 edge 恢复后用旧状态抢写。

建议至少提供这些 authority 字段（名称可按 PLC 习惯调整，但语义需保留）：

| name | 方向 | type | point_role | 说明 |
|---|---|---|---|---|
| CTRL.MODE | PLC -> edge/HMI | int | authority | 当前模式：EDGE_MASTER/HMI_STANDBY/HMI_LOCAL/PLC_LOCAL/LOCAL_MANUAL/MAINTENANCE_LOCK/FAULT_STOP |
| CTRL.AUTHORITY_OWNER | PLC -> edge/HMI | int | authority | 当前控制权 owner：EDGE/HMI/PLC_LOCAL/MAINTENANCE/FAULT |
| CTRL.AUTHORITY_REQUEST | edge/HMI -> PLC | int | authority | 接管/释放请求 |
| CTRL.AUTHORITY_GRANT | PLC -> edge/HMI | int | authority | PLC 对请求的授予/拒绝结果 |
| CTRL.AUTHORITY_TOKEN | PLC -> edge/HMI | int | authority | 控制权代次，写软点时需匹配 |
| HMI.DIRECT_ENABLE | PLC -> HMI/edge | bool | authority | 是否允许 HMI 备用直连写控制软点 |
| CTRL.TAKEOVER_REASON | PLC -> edge/HMI | int | authority | 最近一次接管原因 |
| CTRL.REJECT_CODE | PLC -> edge/HMI | int | authority | 最近一次拒绝原因 |

点表示例（含中文备注）：

| name       | address | function_code | type  | byte_order | scale | unit | range_min | range_max | alarm_low | alarm_high | point_role | 备注                  |
|------------|---------|---------------|-------|------------|-------|------|-----------|-----------|-----------|------------|------------|-----------------------|
| HB.READ    | 50000   | 3             | int   | big        | 1     |      |           |           |           |            | heartbeat  | 心跳读，PLC计数递增   |
| HB.WRITE   | 50001   | 3             | int   | big        | 1     |      |           |           |           |            | heartbeat  | 心跳写，edge 写递增计数 |
| FTT1       | 40010   | 3             | float | big        | 1.0   | °C   |           |           |           |            | readonly_io | 主机1风温采集         |
| VFM1_FREQ_SP | 41020 | 3             | float | big        | 1.0   | Hz   | 0         | 50        |           |            | control_softpoint | VFM1 目标频率软点 |
| CTRL.AUTHORITY_OWNER | 41030 | 3       | int   | big        | 1     |      |           |           |           |            | authority  | 当前控制权 owner      |
| CTRL.AUTHORITY_TOKEN | 41031 | 3       | int   | big        | 1     |      |           |           |           |            | authority  | 控制权代次            |
| HMI.DIRECT_ENABLE | 41032 | 3          | bool  | big        | 1     |      |           |           |           |            | authority  | HMI 直连写入授权      |
| ALARM.CODE | 42010   | 3             | int   | big        | 1     |      |           |           |           |            | alarm      | 当前 PLC 告警码       |

## 7. 联调检查清单
1) 确认站号、端口、功能码、字节序。
2) 提供完整点表，标明可写点。
3) 提供心跳寄存器与心跳丢失切本地状态。
4) 提供参数同步、报警同步、状态同步、无扰切换、HMI/edge 控制权锁和 HMI 直连启用字段。
5) 联调前用测试 PLC/模拟器自查：无重复地址、功能码正确、字节序与类型匹配，ACK/NAK/原因码可复现。
