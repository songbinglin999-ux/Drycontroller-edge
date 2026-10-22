# 数据库迁移（Alembic）— edge-api

本文档说明 edge-api PostgreSQL schema 演进工具与流程。配套云端服务（不在本仓库）使用相同流程。

> **当前阶段：Phase B 完整切换完成 2026-05-14。**
> Alembic 接管 edge-api PostgreSQL schema：容器 entrypoint 跑 `alembic upgrade head` 再启 uvicorn。
> `_ensure_db_schema_forever()` 后台 retry task 已删除（alembic 失败 = 容器起不来 = docker 自然重启）。
> 7 张 ORM 表已就位（详见 `app/models/__init__.py`）：
> alarm_events / operation_audit_logs / control_command_history / ota_operations /
> preflight_runs / config_versions / cloud_sync_outbox
>
> 当前接入状态（2026-05-22 复核）：
> - `operation_audit_logs` / `alarm_events` / `control_command_history` / `ota_operations` /
>   `preflight_runs` / `config_versions`：**active**（producer 同步写本地历史 + `cloud_sync_outbox`，drainer `app/services/outbox_drain.py:CloudSyncOutboxDrainer` 在 `main.py` lifespan 启动，cloud-ops 侧 `*/from-edge` consumer 已就位；见顶层 `architecture-audit-2026-05-06.md` P1-N5）

> **Edge App Release v1 边界：** 日常两镜像 OTA 不得携带 Alembic revision
> 变化。发布 workflow 必须证明受保护数据库 baseline commit 到候选 source
> commit 的 `services/edge-api/alembic/versions/` 零差异。需要迁移时继续使用
> 明确的 SSH 维护窗口、数据库备份和人工验收流程；本期不做自动 downgrade。

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


