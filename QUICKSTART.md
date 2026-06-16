# 🚀 快速开始指南

5 分钟快速启动工业烘干智能控制系统！

## 前置条件

请确保已安装：
- ✅ Docker (>= 20.10)
- ✅ Docker Compose (>= 2.0)

## 步骤 1: 克隆项目

```bash
git clone <repository-url>
cd Drycontroller-edge
```

## 步骤 2: 配置环境

```bash
# 复制环境变量模板
cp env.example .env

# 可选：编辑配置（使用默认配置也可以运行）
# vim .env
```

## 步骤 3: 启动服务

```bash
# 方式 1: 使用启动脚本（推荐）
bash scripts/dev-start.sh

# 方式 2: 直接使用 docker-compose
docker-compose up -d

# 方式 3: 使用 Makefile
make dev
```

等待 30 秒左右，所有服务即可启动完成。

## 步骤 4: 验证服务

### 检查服务状态

```bash
docker-compose ps
```

应该看到所有服务都是 `Up` 状态：

```
NAME                   STATUS
edge-api              Up (healthy)
edge-daq              Up
ai-softsensor         Up
ai-ctrl-optimizer     Up
license-service       Up
edge-monitor          Up
postgres              Up (healthy)
influxdb              Up (healthy)
mosquitto             Up (healthy)
```

### 访问 API 文档

打开浏览器访问：

```
http://localhost:8000/docs
```

您将看到完整的 Swagger API 文档界面。

### 测试 API 接口

```bash
# 健康检查
curl http://localhost:8000/health

# 获取实时数据
curl http://localhost:8000/api/realtime/latest

# 获取系统状态
curl http://localhost:8000/api/v1/system/status
```

## 步骤 5: 查看实时数据

### 方式 1: 通过 API

```bash
# 获取所有实时数据
curl http://localhost:8000/api/realtime/latest | jq

# 获取特定标签（当前 SSOT 使用 io-map V2 位号）
curl "http://localhost:8000/api/realtime/latest?tags=FTT1&tags=VF1" | jq
```

### 方式 2: 订阅 MQTT

```bash
# 安装 MQTT 客户端
# macOS: brew install mosquitto
# Ubuntu: sudo apt-get install mosquitto-clients

# 订阅实时数据
mosquitto_sub -h localhost -t "realtime/+/tags" -v

# 订阅 AI 预测结果
mosquitto_sub -h localhost -t "ai/+/moisture" -v
```

### 方式 3: WebSocket 推送

```bash
# 安装 websocat: brew install websocat

# 实时点位（可加 line/tags 查询参数）
websocat ws://localhost:8000/api/ws/realtime

# AI 推理结果
websocat ws://localhost:8000/api/ws/ai?line=L1

# 系统健康状态
websocat ws://localhost:8000/api/ws/system
```

### 方式 4: 查询 InfluxDB

访问 InfluxDB Web UI：

```
http://localhost:8086
```

登录信息：
- 用户名/密码/组织/Bucket：以 `.env`（由 `env.example` 复制）中的 `INFLUXDB_*` 为准

## 常用命令

### 查看日志

```bash
# 查看所有服务日志
docker-compose logs -f

# 查看特定服务日志
docker-compose logs -f edge-api
docker-compose logs -f edge-daq
docker-compose logs -f ai-softsensor
```

### 重启服务

```bash
# 重启所有服务
docker-compose restart

# 重启特定服务
docker-compose restart edge-api
```

### 停止服务

```bash
# 停止所有服务
docker-compose down

# 停止并删除数据卷
docker-compose down -v
```

## 测试 AI 功能

### 1. 查看 AI 含水率预测

```bash
curl http://localhost:8000/api/v1/ai/moisture/current | jq
```

期望输出：

```json
{
  "success": true,
  "data": {
    "predicted_moisture": 12.3,
    "confidence": 0.92,
    "model_version": "v1.0.0",
    "timestamp": "2025-01-01T12:00:00Z"
  }
}
```

### 2. 获取控制优化建议

```bash
curl http://localhost:8000/api/v1/ai/optimization/recommendation | jq
```

## 测试控制建议与人工确认

```bash
curl http://localhost:8000/api/v1/control/recommendations \
  -H "X-API-Key: $CONTROL_API_KEY" | jq
```

人工确认软点写入必须走编排层；旧 `/api/v1/control/set`、`/batch`、`/emergency-stop`
不再作为公开入口。

```bash
curl -X POST http://localhost:8000/api/v1/control/recommendations/manual-confirm \
  -H "X-API-Key: $CONTROL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "line": "L1",
    "target_softpoint": "VFM1_FREQ_SP",
    "value": 20.0,
    "request_id": "manual-demo-1",
    "operator_id": "operator-local",
    "reason": "hmi_panel_submit"
  }' | jq
```

## 查看监控指标

### 系统状态

```bash
curl http://localhost:8000/api/v1/system/status | jq
```

### 系统资源

```bash
curl http://localhost:8000/api/v1/system/metrics | jq
```

### 活动报警

```bash
curl http://localhost:8000/api/v1/alarms/active | jq
```

## 开发模式

### 修改代码后重启服务

```bash
# 重新构建并启动
docker-compose up -d --build edge-api

# 查看日志
docker-compose logs -f edge-api
```

### 进入容器

```bash
# 进入 API 容器
docker-compose exec edge-api bash

# 进入数据库容器
docker-compose exec postgres psql -U edge_user -d edge_db
```

## 故障排查

### 服务无法启动

```bash
# 检查日志
docker-compose logs

# 清理并重新启动
docker-compose down -v
docker-compose up -d
```

### 端口冲突

```bash
# 修改 .env 文件中的端口配置
vim .env

# 重启服务
docker-compose up -d
```

### 数据库连接失败

```bash
# 检查数据库状态
docker-compose exec postgres pg_isready

# 重启数据库
docker-compose restart postgres
```

## 下一步

🎉 恭喜！系统已经启动成功。

接下来您可以：

1. 📖 阅读完整文档：[README.md](README.md)
2. 🏗️ 了解系统架构：[docs/architecture/system-overview.md](docs/architecture/system-overview.md)
3. 🚢 学习生产部署：[docs/archive/ops/deployment-guide.md](docs/archive/ops/deployment-guide.md)
4. 🔧 配置 PLC 连接：修改 `configs/tags.yaml` 和 `.env` 中的 PLC 配置
5. 🤖 部署 AI 模型：将训练好的 `.onnx` 模型放入 `services/ai-softsensor/models/`

## 获取帮助

- 📧 Email: support@example.com
- 💬 Issue: [GitHub Issues](https://github.com/username/repo/issues)
- 📚 文档: [完整文档](README.md)

---

**享受开发！** 🚀
