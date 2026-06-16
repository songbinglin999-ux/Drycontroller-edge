# 🧱 PLC 接入与联调指南（PLC Integration Guide）

> 版本：v1.0  
> 适用范围：现场首次接入 / 新 PLC 型号联调

---

## 1. 前置条件

- 已确认 PLC 品牌与型号（如 Siemens / Mitsubishi / Omron / 其它）  
- 已确认 PLC 支持 **Modbus TCP Server** 或具备相应网关/模块  
- 已准备：
  - PLC 程序草案 / 变量表  
  - 现场网络拓扑与 IP 规划

---

## 2. 网络与基础配置

1. 为 PLC 分配固定 IP（如 `192.168.10.10`）  
2. 在 `.env` 或 `configs/tags.yaml` 中配置 PLC IP 与端口（默认 502）  
3. 确保工控机与 PLC 处于同一网段或通过工业交换机互通  
4. 使用 `ping` 与 Modbus 调试工具（如 Modbus Poll）验证连通性

---

## 3. 寄存器规划

- 与 PLC 工程师共同规划地址区间：
  - 模拟量过程值  
  - 设定值与控制量  
  - 状态位与报警位  
- 将地址规划结果同步到：
  - `docs/plc/io-and-control-contract.md` / `docs/plc/io-map.html`
  - `configs/tags.yaml`（位号语义）
  - `configs/modbus-address-map.yaml`（地址 / scale / byte_order）

---

## 4. 联调步骤

1. **单点读写测试**
   - 使用通用 Modbus 工具对规划的寄存器进行读写  
   - 验证数据类型与缩放关系（如温度 ×10 存储等）
2. **接入 edge-daq**
   - 启动 `edge-daq` 服务  
   - 查看日志确认是否能稳定读取数据  
   - 在 MQTT 客户端（如 MQTTX）订阅对应 Topic 观察数据
3. **与大屏/可视化联通**
   - 确认 `edge-api` 能通过 InfluxDB / MQTT 获取到工艺数据  
   - 验证大屏上的实时曲线与现场仪表是否一致

---

## 5. 常见问题排查

- 无法连接 PLC：
  - 检查 IP/端口、防火墙、交换机 VLAN 配置  
  - 使用 `tcpdump`/Wireshark 抓包确认是否有 Modbus 报文
- 数据异常：
  - 检查字节序（Big-Endian/Little-Endian）  
  - 检查缩放系数与工程量范围  
  - 检查 PLC 程序中是否正确更新对应寄存器

---

## 6. 后续维护建议

- 任意 PLC 程序变更需同步更新点位表与 `tags.yaml`  
- 对于新产线/设备，尽量复用现有命名与地址规划，以便缩短联调时间  
- 定期对关键传感器做校准，并记录在运维文档中
