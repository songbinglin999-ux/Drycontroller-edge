# 🌐 API 架构与接口分层设计（API Architecture）

> 版本：v1.0  
> 适用范围：`edge-api` 服务及与之对接的所有客户端（大屏、运维工具、第三方系统）

---

## 1. 设计目标

- **统一入口**：所有外部访问统一通过 `edge-api`，内部服务不对外暴露  
- **读写分离**：数据查询与控制操作分开设计，降低耦合与风险  
- **实时与历史并存**：同时支持时序曲线查询与当前状态查询  
- **工业友好**：接口语义清晰，可被 PLC 网关/上位机系统集成  
- **安全可控**：预留鉴权、授权与操作审计能力

---

## 2. 总体架构

```text
外部客户端（大屏/运维工具/第三方系统）
        ↓ HTTP / WebSocket
      edge-api（FastAPI）
        ↓
  ┌────────────┬─────────────┬─────────────┬─────────────┐
  ↓            ↓             ↓             ↓
PostgreSQL   InfluxDB      MQTT Broker   其它微服务（未来）
```

`edge-api` 职责：

- 聚合多源数据（时序 + 业务 + 实时 MQTT）  
- 封装统一 REST / WebSocket 接口  
- 提供统一的健康检查与系统状态视图  
- 作为未来对外 **网关** 的核心后端

---

## 3. 接口分层

### 3.1 路由分组

`edge-api/app/api/v1/` 下建议划分为以下路由模块：

- `data.py`：数据查询（实时 + 历史）  
- `control.py`：控制指令与策略相关操作  
- `ai.py`：AI 结果查询、状态监控  
- `alarms.py`：报警查询与确认  
- `system.py`：系统状态、健康检查、版本信息

### 3.2 分层结构

建议采用典型三层架构：

- **API 层（Router / Schema）**
  - 负责请求验证、响应序列化
- **Service 层（业务服务）**
  - 封装具体业务逻辑、与数据源交互
- **Repository / Client 层**
  - 具体的数据访问实现（PostgreSQL / InfluxDB / MQTT）

---

## 4. 核心 API 设计

> 以下路径以 `/api/v1` 为前缀。

### 4.1 数据查询接口

- **实时数据**

  - `GET /data/realtime`
  - 功能：返回当前产线关键点位的实时值  
  - 典型请求参数：
    - `tags`: 逗号分隔的点位名（可选，默认返回核心点位）
  - 返回示例：

  ```json
  {
    "timestamp": "2025-01-01T12:00:00Z",
    "values": {
      "T_ZONE1": 85.2,
      "T_ZONE2": 88.5,
      "MOISTURE_EST": 12.3
    }
  }
  ```

- **历史数据**

  - `GET /data/history`
  - 功能：查询指定时间范围内的历史曲线（来自 InfluxDB）  
  - 典型请求参数：
    - `tags`: 点位列表  
    - `start`: 起始时间  
    - `end`: 结束时间  
    - `interval`: 聚合间隔（如 `1m`、`5m`）

### 4.2 控制接口

- **下发控制指令**

  - `POST /control/set`
  - 功能：提交对链速、风机频率等控制建议的“应用”请求  
  - 请求示例：

  ```json
  {
    "target": "line1",
    "commands": [
      { "tag": "CHAIN_SPEED_SETPOINT", "value": 1.8 },
      { "tag": "FAN_FREQ_SETPOINT", "value": 35.0 }
    ],
    "mode": "manual_confirm",   // 预留：未来支持 auto
    "operator": "operator_a"
  }
  ```

  - 处理逻辑：
    - 写入 PostgreSQL 控制操作日志  
    - 通过 MQTT 将控制命令发送给 `edge-daq`  
    - 由 `edge-daq` 将对应值写入 PLC

### 4.3 AI 接口

- `GET /ai/moisture/current`
  - 功能：获取当前 AI 含水率估计值及置信度  
- `GET /ai/status`
  - 功能：查询 AI 服务当前状态（模型版本、健康、是否降级）

### 4.4 报警与系统接口

- `GET /alarms/active`
  - 功能：查询当前未恢复的报警列表  
- `POST /alarms/ack`
  - 功能：对指定报警进行确认（ack）  
- `GET /system/status`
  - 功能：返回各服务健康状态、版本、资源使用概览  
- `GET /system/health`
  - 功能：简单健康检查（用于反向代理 / 负载均衡）

---

## 5. WebSocket 实时通道

### 5.1 连接端点

- `GET /ws/realtime`
  - 用于大屏订阅实时数据与事件流  
  - 建议支持以下订阅类型：
    - `realtime_data`：实时工艺点位  
    - `ai_moisture`：软测量结果  
    - `system_events`：自愈、OTA、服务状态变更事件

### 5.2 消息格式

```json
{
  "type": "realtime_data | ai_moisture | system_event",
  "timestamp": "2025-01-01T12:00:00Z",
  "payload": {
    "...": "..."
  }
}
```

---

## 6. 安全与授权（预留）

> 当前阶段可根据实际交付需求选择是否启用完整鉴权；在内网部署且有物理隔离时可简化。

- 支持的安全机制：
  - Token / API Key 校验  
  - IP 白名单  
  - 与 `license-service` 集成的功能开关（如 AI 功能是否可用）
- 要求：
  - 任何修改配置、执行控制的接口必须记录**操作人、时间、IP**  
  - 所有敏感接口应具备**最小权限控制**能力（未来可与用户体系集成）

---

## 7. 与内部服务的关系

- `edge-api` 不直接做 PLC 通信与 AI 推理：
  - PLC 通信统一交给 `edge-daq`  
  - AI 推理由 `ai-softsensor` 完成  
- `edge-api` 只做：
  - 调用 PostgreSQL / InfluxDB 获取历史与状态  
  - 通过 MQTT 读写实时数据 / 控制消息  
  - 与 `license-service` 交互获取授权状态

这种设计确保：

- API 层无状态、易扩展（可横向扩容）  
- 业务边界清晰，便于不同团队协作开发与维护

---

## 8. 版本管理与演进

- 所有接口统一前缀：`/api/v1`  
- 不兼容变更流程：
  - 新增 `/api/v2` 路由组  
  - 保留 `/api/v1` 一段时间供旧版前端使用  
  - 在文档中标记废弃接口与替代方案

未来可扩展：

- 引入 OpenAPI 规范驱动的前后端代码自动生成  
- 对接 API 网关（如 Nginx / Kong）做限流、熔断与审计


