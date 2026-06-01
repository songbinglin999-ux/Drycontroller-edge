#!/bin/sh
# P2-N8: 迁移前自动 pg_dump + 失败 fail-closed (edge-api 版)
# 用法: ENTRYPOINT ["/app/entrypoint.sh"]
# 依赖: DATABASE_URL；postgresql-client（pg_dump，Dockerfile 已装）；SQLAlchemy
#
# 流程:
#   1. 解析 DATABASE_URL（SQLAlchemy make_url，兼容 postgresql+asyncpg 与特殊字符密码）
#   2. 检测 pending migration
#   3. postgresql 后端且有 pending → pg_dump 备份；备份失败即中止（fail-closed）
#   4. alembic upgrade head；失败保留备份并退出非零
#   5. 成功 → exec 启动命令
#
# 备份默认落 /logs/edge-api（edge-api 唯一现成的持久 bind mount，容器重建后仍在）；
# 语义上是迁移备份而非日志，如需独立卷用 MIGRATION_BACKUP_DIR 覆盖并在 compose 挂载。

set -e

BACKUP_DIR="${MIGRATION_BACKUP_DIR:-/logs/edge-api}"
DB_URL="${DATABASE_URL:-}"

# 用 SQLAlchemy 解析 DATABASE_URL，导出 pg_dump 所需的 PG* 环境变量。
# 仅 postgresql 后端输出内容；其它后端（sqlite 等）输出空 → 跳过备份。
parse_pg_env() {
  python - "$DB_URL" <<'PY'
import sys
try:
    from sqlalchemy.engine import make_url
    u = make_url(sys.argv[1])
except Exception:
    sys.exit(0)
if not u.get_backend_name().startswith("postgresql"):
    sys.exit(0)
print("PGHOST=%s" % (u.host or ""))
print("PGPORT=%s" % (u.port or 5432))
print("PGUSER=%s" % (u.username or ""))
print("PGDATABASE=%s" % (u.database or ""))
print("PGPASSWORD=%s" % (u.password or ""))
PY
}

IS_PG=0

load_pg_env() {
  [ -z "$DB_URL" ] && { echo "DATABASE_URL not set, skip pre-migration backup"; return 0; }
  pg_env=$(parse_pg_env)
  [ -z "$pg_env" ] && { echo "DATABASE_URL is not a postgresql backend, skip pg_dump backup"; return 0; }
  while IFS= read -r line; do
    [ -n "$line" ] && export "$line"
  done <<EOF
$pg_env
EOF
  IS_PG=1
}

has_pending_migration() {
  current=$(alembic current 2>/dev/null | head -1 | awk '{print $1}')
  head_rev=$(alembic heads 2>/dev/null | head -1 | awk '{print $1}')
  if [ -n "$current" ] && [ "$current" = "$head_rev" ]; then
    echo "alembic: already at head ($current), skip backup"
    return 1
  fi
  echo "alembic: pending migration detected (current=${current:-none} head=${head_rev:-unknown})"
  return 0
}

pre_migration_backup() {
  load_pg_env
  has_pending_migration || return 0
  [ "$IS_PG" -eq 1 ] || { echo "non-postgresql backend, proceeding without pg_dump"; return 0; }

  mkdir -p "$BACKUP_DIR"
  ts=$(date +%Y%m%d_%H%M%S)
  backup_file="$BACKUP_DIR/pre-migration-${ts}.sql.gz"
  echo "alembic: backing up database to $backup_file"

  # fail-closed：备份失败就不迁移。dash 无 pipefail，`pg_dump | gzip` 退出码只反映 gzip
  # （pg_dump 失败时 gzip 仍把空输入压成存档并返回 0），故先 dump 到文件捕获真实退出码再压缩。
  dump_sql="$BACKUP_DIR/pre-migration-${ts}.sql"
  if pg_dump --no-owner --no-acl > "$dump_sql"; then
    gzip -f "$dump_sql"
    sz=$(wc -c < "$backup_file" | tr -d ' ')
    echo "alembic: backup complete ($sz bytes)"
    echo "$backup_file" > "$BACKUP_DIR/last-pre-migration-backup"
  else
    rm -f "$dump_sql"
    echo "ERROR: pg_dump failed — aborting before migration (fail-closed)"
    exit 1
  fi
}

run_migration() {
  echo "alembic: running upgrade head..."
  if alembic upgrade head; then
    echo "alembic: migration succeeded"
    return 0
  fi

  echo "ERROR: alembic upgrade head FAILED"
  if [ -f "$BACKUP_DIR/last-pre-migration-backup" ]; then
    backup_file=$(cat "$BACKUP_DIR/last-pre-migration-backup")
    echo "Pre-migration backup available at: $backup_file"
    echo "To restore: zcat $backup_file | psql \$DATABASE_URL"
  fi
  exit 1
}

pre_migration_backup
run_migration

echo "Starting application: $*"
exec "$@"
