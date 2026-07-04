# 大屏集成指南

## 1. 数据来源总览

| 数据类型 | 推荐接口 | 说明 |
|----------|----------|------|
| 最新点位 / 历史曲线 | REST：`/api/v1/data/latest`、`/api/v1/data/history` | 用于页面初始化或回放 |
| 实时点位推送 | WebSocket：`/api/ws/realtime` | 可按 `line`、`tags` 过滤 |
| AI 推理结果 | WebSocket：`/api/ws/ai` | 含 `value`、`confidence`、`model_version` |
| 系统健康状态 | WebSocket：`/api/ws/system` / REST：`/api/system/health` | 监控整体状况 |

## 2. 系统状态总览 Schema

- MQTT Topic：`system/<line>/status`（例如 `system/L1/status`）  
- REST 兜底：`GET http://edge-monitor:8080/system/health`（返回同样结构）
- Schema 固定为 `edge.monitor.status.v1`，字段如下：

| 字段 | 类型 | 说明 |
|------|------|------|
| `schema` | string | 固定值 `edge.monitor.status.v1` |
| `line` | string | 产线 ID，例如 `L1` |
| `timestamp` | int | Unix 秒，消息生成时间 |
| `system.metrics` | object | CPU、内存、磁盘、uptime、updated_at |
| `services` | array | 每个元素描述一个服务 |

`services` 数组中的结构：

| 字段 | 说明 |
|------|------|
| `name` | 与 `configs/monitor.yaml` 中的 `targets.name` 对齐 |
| `status` | `healthy` / `degraded` / `down` |
| `checks` | Map，包含 `http` / `tcp` / `container` 等详细探测结果 |
| `latency_ms` | 来自 HTTP 或 TCP 的探测延迟（若可用） |
| `container_state` | Docker 状态，如 `running`、`exited` |
| `updated_at` | 最近一次检查的 Unix 秒 |

示例载荷：

```json
{
  "schema": "edge.monitor.status.v1",
  "line": "L1",
  "timestamp": 1732599451,
  "system": {
    "metrics": { "cpu": { "percent": 12.5 }, "memory": { "percent": 48.2 }, "disk": { "percent": 61.0 }, "uptime_sec": 32450 },
    "updated_at": 1732599451
  },
  "services": [
    {
      "name": "edge-api",
      "status": "healthy",
      "latency_ms": 2.4,
      "container_state": "running",
      "updated_at": 1732599450,
      "checks": {
        "http": { "status": "healthy", "status_code": 200, "latency_ms": 2.4 },
        "container": { "status": "healthy", "state": "running" }
      }
    },
    {
      "name": "edge-mosquitto",
      "status": "healthy",
      "checks": {
        "tcp": { "status": "healthy", "latency_ms": 1.1, "host": "edge-mosquitto", "port": 1883 },
        "container": { "status": "healthy", "state": "running" }
      },
      "updated_at": 1732599450
    }
  ]
}
```

> 若某服务未配置 HTTP/TCP 检查，会自动标记为 `degraded` 并提示 `no_checks_configured`，因此大屏不再需要处理 `unknown` 状态。

## 3. WebSocket 接入示例

```javascript
const realtimeSocket = new WebSocket("ws://edge-api:8000/api/ws/realtime?line=L1");

realtimeSocket.onmessage = (event) => {
  const payload = JSON.parse(event.data);
  // payload = { line, tag, value, unit, ts, topic }
  renderRealtimePoint(payload);
};

realtimeSocket.onclose = () => setTimeout(() => reconnect(), 3000);
```

- 若只关心少量点位，可追加 `&tags=FTT1,T_OUT2`；
- AI、系统健康同理，分别监听 `/api/ws/ai`、`/api/ws/system`。

## 4. 与 MQTT 对齐

WebSocket 消息与 MQTT 载荷完全一致，字段说明：

| 字段 | 说明 |
|------|------|
| `line` | 产线/工位，如 `L1` |
| `tag` | `configs/tags.yaml` 中定义的点位名 |
| `value` | 实时数值 |
| `unit` | 单位，可为空字符串 |
| `ts` | Unix 秒时间戳 |
| `confidence` | 仅 AI 结果包含 |
| `topic` | MQTT 原始 Topic，便于调试 |

## 5. 常见模式

1. **页面初始化**  
   - REST 获取当前 `latest` 数据填入全局状态；  
   - 启动 WebSocket 持续更新。

2. **历史回放**  
   - 调用 `/api/v1/data/history?tag=FTT1&period=6h&window=1m` 获取折线；
   - 若需 AI 历史，调用 `/api/v1/ai/moisture/history`。

3. **系统告警**  
   - 订阅 `/api/ws/system`，监听 `services.edge-api.status != healthy`；  
   - 或定时请求 `/api/system/health` 作为兜底。

## 6. 错误处理建议

| 场景 | 建议处理 |
|------|----------|
| WebSocket 断开 | 指数退避重连；重连后重新请求 `/api/v1/data/latest` 补齐丢失数据 |
| 无历史数据 | 提示“暂无数据”，并记录请求参数便于排障 |
| API 429/503 | 实现简单地退避重试，并将告警报到 edge-monitor |

## 7. 调试清单

- `websocat ws://localhost:8000/api/ws/realtime` 验证实时推送；
- `curl http://localhost:8000/api/system/health` 检查系统缓存；
- `docker compose logs -f edge-api edge-daq ai-softsensor edge-monitor` 查看链路日志。
