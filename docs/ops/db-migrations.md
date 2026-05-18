# 数据库迁移（Alembic）— edge-api

本文档说明 edge-api PostgreSQL schema 演进工具与流程。**核心流程与 cloudserver 一致**——完整版见 [`Drycontroller-cloudserver/docs/ops/db-migrations.md`](https://git.example.com/example-org/Drycontroller-cloudserver/src/branch/main/docs/ops/db-migrations.md)，本文档只记录 edge-api 的差异点。

> **当前阶段：Phase B 完整切换完成 2026-05-14。**
> Alembic 接管 edge-api PostgreSQL schema：容器 entrypoint 跑 `alembic upgrade head` 再启 uvicorn。
> `_ensure_db_schema_forever()` 后台 retry task 已删除（alembic 失败 = 容器起不来 = docker 自然重启）。
> 7 张 ORM 表已就位（详见 `app/models/__init__.py`）：
> alarm_events / operation_audit_logs / control_command_history / ota_operations /
> preflight_runs / config_versions / cloud_sync_outbox
>
> 当前接入状态（2026-05-18 复核）：
> - `operation_audit_logs` + `cloud_sync_outbox`：**active**（audit 域 producer `app/services/audit.py:write_audit()` 同时写两表，drainer `app/services/outbox_drain.py:CloudSyncOutboxDrainer` 在 `main.py` lifespan 启动；cloud-ops 侧 `POST /api/v1/audit/from-edge` consumer 已就位）
> - `alarm_events` / `control_command_history` / `ota_operations` /
>   `preflight_runs` / `config_versions`：schema-only（producer 尚未接 outbox，断网时事件仍可能丢；见顶层 `architecture-audit-2026-05-06.md` P1-N5）

---

## edge-api 与 cloud-ops 的差异点

| 项 | cloud-ops | edge-api |
|---|---|---|
| `Base` 位置 | `app/models.py` | `app/core/database.py` |
| `DATABASE_URL` 形式 | `settings.DATABASE_URL`（属性，大写） | `settings.database_url`（property，小写）|
| 表数量 | 8 张（Node / Job / JobItem / AuditLog / User / Tenant / Device / LicenseBinding） | 7 张（alarm_events / operation_audit_logs / control_command_history / ota_operations / preflight_runs / config_versions / cloud_sync_outbox）|
| Startup schema 创建 | entrypoint `alembic upgrade head` 接管；`_init_db()` 仅做种子 | entrypoint `alembic upgrade head` 接管；`_ensure_db_schema_forever()` 已删除 |
| alembic 包 | `alembic==1.14.0` | `alembic==1.13.1` |

`alembic/env.py` 的 import 路径已对应：

```python
from app.core.config import settings
from app.core.database import Base   # ← edge-api 是 core/database.py，不是 models.py

config.set_main_option("sqlalchemy.url", settings.database_url)  # ← 小写 property
```

---

## 对应关系

详见顶层 [`p1-roadmap-2026-05-07.md`](../../../p1-roadmap-2026-05-07.md) **P1-5 / 5.7**。
