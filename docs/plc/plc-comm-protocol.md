# PLC 通讯协议（给 PLC 工程师）

只保留 PLC 侧需要做的事情，其他上位链路细节已去掉。

## 1. 通讯参数
- 协议：Modbus TCP
- 站号：默认 1（若不同请告知）
- 端口：默认 502
- 功能码：读 3（保持寄存器）、读 4（输入寄存器）；写 6/16（单/多寄存器）
- 字节序：默认 Big Endian（如少量点需 Little，请在表中标注）

## 2. 点表字段（请按表格提供）
- 必填：`name`（唯一）、`address`、`function_code`（3/4）、`type`（float/int/int32/uint32/bool）
- 可选：`byte_order`(big/little)、`scale`（工程值 = 原值 * scale）、`unit`、`range[min,max]`、`alarm_low`、`alarm_high`
- 控制写入的点：在表中注明需要可写（我们会标 `control_enabled=true`）
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
| control_enabled | 是否可写          | true/false |

## 3. 读取规则（PLC 侧无需改逻辑，只需点表正确）
- `float`：占 2 寄存器，按 `byte_order` 组字
- `int32`/`uint32`：占 2 寄存器
- `int`/`bool`：占 1 寄存器（bool 非 0 即 True）
- 我们会自动合并连续地址批量读取（同功能码、同字节序、同寄存器长度）。

## 4. 写入规则（控制点）
- 仅 `control_enabled=true` 的点会被写
- 寄存器占用：`float`/`int32`/`uint32` 用 2 个寄存器；`int`/`bool` 用 1 个寄存器（bool 写 0/1）
- 如配置 `scale`：寄存器值 = 工程值 / scale
- 如配置 `range`：超出范围将被拒绝下发

## 5. 心跳（可选）
- 可提供读心跳寄存器（如 50000）和/或写心跳寄存器（如 50001）
- 读：PLC 侧计数递增；写：我们周期写递增计数
- 请在点表注明心跳地址与功能码

心跳推荐：
- 功能码：3（保持寄存器）
- 周期：5s（可调）
- 判定：连续 2~3 次读值不变或读/写失败，判定通讯异常
- 计数回绕：计数达上限可回到 0（无需特殊处理）

点表示例（含中文备注）：

| name       | address | function_code | type  | byte_order | scale | unit | range_min | range_max | alarm_low | alarm_high | control_enabled | 备注                  |
|------------|---------|---------------|-------|------------|-------|------|-----------|-----------|-----------|------------|-----------------|-----------------------|
| HB.READ    | 50000   | 3             | int   | big        | 1     |      |           |           |           |            | false           | 心跳读，PLC计数递增   |
| HB.WRITE   | 50001   | 3             | int   | big        | 1     |      |           |           |           |            | false           | 心跳写，我们写递增计数 |
| TEMP.PV    | 40010   | 3             | float | big        | 1.0   | °C   |           |           |           |            | false           | 工艺温度采集         |
| TEMP.SV    | 40020   | 3             | float | big        | 1.0   | °C   |           |           |           |            | true            | 温度设定值，可写     |
| PUMP.RUN   | 40030   | 3             | bool  | big        | 1     |      |           |           |           |            | true            | 泵启停位，可写 0/1   |

## 6. 联调检查清单
1) 确认站号、端口、功能码、字节序。
2) 提供完整点表，标明可写点。
3) 可选提供心跳寄存器。
4) 联调前用测试 PLC/模拟器自查：无重复地址、功能码正确、字节序与类型匹配。

