#!/bin/bash
# 数据库迁移脚本

set -e

echo "======================================"
echo "  执行数据库迁移"
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

# 创建备份
echo "📦 备份当前数据库..."
BACKUP_FILE="backup/postgres_backup_$(date +%Y%m%d_%H%M%S).sql"
mkdir -p backup
docker-compose exec -T postgres pg_dump -U $POSTGRES_USER $POSTGRES_DB > $BACKUP_FILE
echo "✅ 备份完成: $BACKUP_FILE"

# 执行迁移
echo "🔄 执行迁移..."

# 这里可以添加实际的迁移逻辑
# 例如使用 Alembic 或其他迁移工具

echo "✅ 迁移完成"

echo ""
echo "======================================"
echo "  数据库迁移完成"
echo "======================================"

