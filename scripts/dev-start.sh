#!/bin/bash
# 开发环境启动脚本

set -e

echo "======================================"
echo "  启动边缘计算开发环境"
echo "======================================"

# 检查 .env 文件
if [ ! -f .env ]; then
    echo "⚠️  .env 文件不存在，从 .env.example 复制..."
    cp .env.example .env
    echo "✅ 已创建 .env 文件，请根据需要修改配置"
fi

# 检查 Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker 未安装，请先安装 Docker"
    exit 1
fi

if ! command -v docker-compose &> /dev/null; then
    echo "❌ docker-compose 未安装，请先安装 docker-compose"
    exit 1
fi

# 创建必要的目录
echo "📁 创建日志目录..."
mkdir -p logs/edge-api logs/edge-daq logs/ai-softsensor logs/ai-ctrl-optimizer logs/license-service logs/edge-monitor

# 创建模型目录
echo "📁 创建模型目录..."
mkdir -p services/ai-softsensor/models services/ai-ctrl-optimizer/models

# 生成 MQTT 密码文件
if [ ! -f infra/mosquitto/passwords.generated ]; then
    echo "🔐 生成 MQTT 密码..."
    # 这里需要 mosquitto_passwd 工具，如果没有则跳过
    if command -v mosquitto_passwd &> /dev/null; then
        mosquitto_passwd -b infra/mosquitto/passwords edge_mqtt mqtt_password_2025
        touch infra/mosquitto/passwords.generated
    else
        echo "⚠️  mosquitto_passwd 工具未找到，跳过密码生成"
    fi
fi

# 停止并清理现有容器
echo "🧹 清理现有容器..."
docker-compose down -v 2>/dev/null || true

# 构建镜像
echo "🔨 构建 Docker 镜像..."
docker-compose build

# 启动服务
echo "🚀 启动服务..."
docker-compose up -d

# 等待服务启动
echo "⏳ 等待服务启动..."
sleep 10

# 检查服务状态
echo ""
echo "======================================"
echo "  服务状态检查"
echo "======================================"
docker-compose ps

# 显示访问信息
echo ""
echo "======================================"
echo "  服务访问信息"
echo "======================================"
echo "📊 边缘 API:          http://localhost:8000"
echo "📊 API 文档:          http://localhost:8000/docs"
echo "🗄️  PostgreSQL:       localhost:5432"
echo "📈 InfluxDB:          http://localhost:8086"
echo "🔌 MQTT Broker:       localhost:1883"
echo "🌐 MQTT WebSocket:    ws://localhost:9001"
echo ""
echo "======================================"
echo "  常用命令"
echo "======================================"
echo "查看日志:   docker-compose logs -f [服务名]"
echo "停止服务:   docker-compose down"
echo "重启服务:   docker-compose restart [服务名]"
echo ""
echo "✅ 开发环境启动完成！"

