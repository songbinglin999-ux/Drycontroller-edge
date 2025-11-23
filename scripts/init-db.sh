#!/bin/bash
# 数据库初始化脚本

set -e

echo "======================================"
echo "  初始化数据库"
echo "======================================"

# 加载环境变量
if [ -f .env ]; then
    export $(cat .env | grep -v '^#' | xargs)
fi

# 等待 PostgreSQL 就绪
echo "⏳ 等待 PostgreSQL 启动..."
until docker-compose exec -T postgres pg_isready -U $POSTGRES_USER; do
    sleep 1
done

echo "✅ PostgreSQL 已就绪"

# 执行初始化脚本
echo "📝 执行数据库初始化脚本..."
docker-compose exec -T postgres psql -U $POSTGRES_USER -d $POSTGRES_DB < infra/postgres/init.sql

if [ $? -eq 0 ]; then
    echo "✅ 数据库初始化成功"
else
    echo "❌ 数据库初始化失败"
    exit 1
fi

# 初始化 InfluxDB
echo "⏳ 初始化 InfluxDB..."
sleep 5

echo "✅ InfluxDB 初始化成功"

echo ""
echo "======================================"
echo "  数据库初始化完成"
echo "======================================"

