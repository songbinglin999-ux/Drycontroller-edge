# 💾 备份与恢复方案（Backup Plan）

> 版本：v1.0  
> 关联脚本：`scripts/init-db.sh` 等  
> 关联文档：`deployment-guide.md`

---

## 1. 备份目标

- 在设备故障或误操作情况下，能够 **在可接受时间内恢复** 到可用状态  
- 确保以下关键数据不丢失或损失可控：
  - PostgreSQL 业务数据  
  - 配置文件与环境变量  
  - （可选）InfluxDB 时序数据

---

## 2. 备份范围

- 必须备份：
  - PostgreSQL：`edge_db` 全库  
  - 配置文件：`configs/*.yaml`、`.env`  
  - 自定义脚本：`scripts/*.sh`（如有本地修改）
- 可选备份：
  - InfluxDB 中重要测点的历史数据（按项目需要）

---

## 3. 备份策略

### 3.1 备份频率

- 配置与脚本：有变更时立即备份 + 每周全量备份  
- PostgreSQL：
  - 每日一次全量备份  
  - 如有需要，可增加增量/归档日志备份

### 3.2 备份位置

- 本地目录（如 `/backup/`）  
- 建议增加：
  - 外接移动硬盘或 NAS（防止本机硬件损坏）  
  - 按需拷贝到企业内部备份服务器

---

## 4. 示例备份脚本

可参考 `deployment-guide.md` 中的脚本，典型流程：

```bash
BACKUP_DIR="/backup/$(date +%Y%m%d)"
mkdir -p $BACKUP_DIR

# 备份 PostgreSQL
docker-compose exec -T postgres pg_dump -U edge_user edge_db > $BACKUP_DIR/postgres.sql

# 备份配置
cp -r configs $BACKUP_DIR/
cp .env $BACKUP_DIR/

# 保留 30 天
find /backup -type d -mtime +30 -exec rm -rf {} \;
```

现场可根据实际情况调整保留周期。

---

## 5. 恢复流程

### 5.1 数据库恢复

```bash
# PostgreSQL
docker-compose exec -T postgres psql -U edge_user edge_db < postgres.sql
```

### 5.2 配置恢复

```bash
cp -r backup/configs/* configs/
cp backup/.env .env
docker-compose restart
```

恢复后需：

- 再次验证服务状态与关键功能  
- 确认配置与版本与预期一致

---

## 6. 定期演练

- 建议每季度至少进行一次 **备份恢复演练**：
  - 在测试环境中按照文档执行完整恢复流程  
  - 记录耗时与潜在问题  
  - 更新本备份方案文档

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