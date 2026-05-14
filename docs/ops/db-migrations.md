# 数据库迁移（Alembic）— edge-api

本文档说明 edge-api PostgreSQL schema 演进工具与流程。**核心流程与 cloudserver 一致**——完整版见 [`Drycontroller-cloudserver/docs/ops/db-migrations.md`](https://git.example.com/example-org/Drycontroller-cloudserver/src/branch/main/docs/ops/db-migrations.md)，本文档只记录 edge-api 的差异点。

> **当前阶段：Phase B 完整切换完成 2026-05-14。**
> Alembic 接管 edge-api PostgreSQL schema：容器 entrypoint 跑 `alembic upgrade head` 再启 uvicorn。
> `_ensure_db_schema_forever()` 后台 retry task 已删除（alembic 失败 = 容器起不来 = docker 自然重启）。
> 7 张 ORM 表已就位（详见 `app/models/__init__.py`）：
> alarm_events / operation_audit_logs / control_command_history / ota_operations /
> preflight_runs / config_versions / cloud_sync_outbox

---

## edge-api 与 cloud-ops 的差异点

| 项 | cloud-ops | edge-api |
|---|---|---|
| `Base` 位置 | `app/models.py` | `app/core/database.py` |
| `DATABASE_URL` 形式 | `settings.DATABASE_URL`（属性，大写） | `settings.database_url`（property，小写）|
| 表数量 | 8 张（Node / Job / JobItem / AuditLog / User / Tenant / Device / LicenseBinding） | **0 张**（`Base.metadata` 空） |
| Startup schema 创建 | `_init_db()` 同步 + `_ensure_schema()` 兜底 ALTER | `_ensure_db_schema_forever()` 后台 task 指数退避重试 |
| alembic 包 | `alembic==1.14.0` | `alembic==1.13.1`（早期已添加，长期未使用）|

`alembic/env.py` 的 import 路径已对应：

```python
from app.core.config import settings
from app.core.database import Base   # ← edge-api 是 core/database.py，不是 models.py

config.set_main_option("sqlalchemy.url", settings.database_url)  # ← 小写 property
```

---

## 现状提醒（careful）

edge-api 的 ORM 基础设施齐备但**没人用**——`engine`、`Base`、`AsyncSessionLocal`、`get_db()` 都已声明，但全代码库无 `class X(Base)`。这意味着：

1. **`Base.metadata.create_all()` 创建 0 张表** —— `_ensure_db_schema_forever()` 实际上是 no-op（除了启动时的"建表"日志没意义）
2. **alembic autogenerate 在加第一张表前会生成空 migration**——不要 commit 空 migration
3. **加第一张表时，本工具立即可用**——同 cloudserver 的 db-migrations.md "1.1 生成 first migration" 流程，但临时 DB 用 edge_db / edge_user 凑

---

## 阶段 B 切换（与 cloud-ops 一致）

- 在 staging 跑 `alembic revision --autogenerate -m "initial schema"` 生成第一份 migration
- 修改 `services/edge-api/Dockerfile` 启动入口：先 `alembic upgrade head` 再启 uvicorn
- 删 `_ensure_db_schema_forever()` 的兜底逻辑

详细 SOP、风险、回滚见 cloudserver 仓库 `docs/ops/db-migrations.md`。

---

## 5. 对应关系

详见顶层 [`p1-roadmap-2026-05-07.md`](../../../p1-roadmap-2026-05-07.md) **P1-5 / 5.7**。
