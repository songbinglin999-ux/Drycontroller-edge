# 🔌 Modbus 映射与 PLC 集成说明（Modbus Mapping & Integration）

> 版本：v1.0  
> 关联服务：`edge-daq`  
> 关联配置：`configs/tags.yaml`

---

## 1. 通信整体说明

- 协议：**Modbus TCP**  
- 通信模式：`edge-daq` 为 Modbus 客户端，PLC 为服务器  
- 功能码：
  - 读取保持寄存器（功能码 3）  
  - 写多个保持寄存器（功能码 16）——用于控制指令下发

---

## 2. 地址映射原则

- 将 PLC 寄存器地址抽象为 **逻辑 Tag**，通过 `configs/tags.yaml` 管理  
- 物理地址变动时，只需修改配置，无需改代码  
- 地址区间建议按功能划分：
  - 40001–40999：工艺过程值（温度、速度等）  
  - 41001–41999：设定值（Setpoint）  
  - 42001–42999：命令与状态  
  - 43001–43999：报警与故障码

---

## 3. 示例映射表（示意）

> 实际完整表建议维护在 Excel/配置系统中，这里列示部分典型点位。

| Tag 名                         | PLC 地址 | 类型   | 单位 | 说明                   |
|-------------------------------|----------|--------|------|------------------------|
| `L1_DRY1_ZONE1_T_PV`          | 40001    | float  | ℃    | 1 线一段 1 区温度      |
| `L1_DRY1_ZONE2_T_PV`          | 40003    | float  | ℃    | 1 线一段 2 区温度      |
| `L1_DRY2_CHAIN_SPEED_PV`      | 40011    | float  | m/min| 1 线二段链速过程值     |
| `L1_DRY2_CHAIN_SPEED_SP`      | 41011    | float  | m/min| 1 线二段链速设定值     |
| `L1_FAN1_FREQ_PV`             | 40021    | float  | Hz   | 抽风机 1 频率过程值    |
| `L1_FAN1_FREQ_SP`             | 41021    | float  | Hz   | 抽风机 1 频率设定值    |
| `L1_AUGER_TORQUE_PV`          | 40031    | float  | Nm   | 料斗绞龙扭矩           |
| `L1_SYSTEM_FAULT_ALM`         | 43001    | uint16 | -    | 系统一般故障报警位     |

---

## 4. `configs/tags.yaml` 中的配置字段

建议每个点位包含以下字段：

- `name`：逻辑 Tag 名（与 `tag-definition-standard.md` 对齐）  
- `plc_address`：Modbus 寄存器地址  
- `function_code`：3/4/16 等  
- `type`：`int16` / `uint16` / `float` / `bool` 等  
- `scale`：缩放系数（如 `0.1` 表示实际值=寄存器*0.1）  
- `unit`：物理单位  
- `rw`：`R` 只读 / `W` 只写 / `RW` 读写  
- `mqtt_topic`：对应 MQTT Topic  
- `description`：中文说明

---

## 5. edge-daq 集成流程

### 5.1 读取流程

1. 启动时读取 `configs/tags.yaml`，构建轮询列表  
2. 按点位类型和地址区间合并读请求，降低 Modbus 报文数量  
3. 对读取值应用缩放与类型转换  
4. 将结果封装为统一数据结构并发布到 MQTT

### 5.2 写入流程（控制下发）

1. 上层（`edge-api` / 大屏）将控制命令转成 Tag + Value  
2. `edge-daq` 接收 MQTT 控制消息  
3. 根据 Tag 查找对应 PLC 地址与缩放系数  
4. 使用功能码 16 写寄存器，实现对 PLC 的控制

---

## 6. 测试与模拟

- 在无 PLC 设备时，可通过 `edge-daq/simulator.py` 启动模拟器：  
  - 模拟产生温度/链速等数据  
  - 验证数据流、AI 推理与大屏展示链路是否正常

---

## 7. 变更管理

- 任何 PLC 地址或类型变更必须：
  - 在 `tags.yaml` 中更新  
  - 更新本文档 / Excel 点位表  
  - 与算法/大屏团队同步，避免因 Tag 变化导致模型特征错误

{
  "cells": [],
  "metadata": {
    "language_info": {
      "name": "python"
    }
  },
  "nbformat": 4,
  "nbformat_minor": 2
}