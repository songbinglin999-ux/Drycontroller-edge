"""
Alembic environment for edge-api (async SQLAlchemy + asyncpg).

Phase A: tool only. edge-api currently has no ORM tables (Base.metadata
is empty); _ensure_db_schema_forever() in app/main.py creates 0 tables.
This file is in place so when edge-api adds its first ORM table, the
team can immediately `alembic revision --autogenerate` instead of
bolting alembic on at the same time as the first model.

See docs/ops/db-migrations.md for the workflow.
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Make `from app.core.database import Base` resolvable when `alembic` is
# run from services/edge-api/ (alembic.ini sets prepend_sys_path = .).
from app.core.config import settings  # noqa: E402
from app.core.database import Base  # noqa: E402

config = context.config

# Override the URL placeholder in alembic.ini with the live database_url
# resolved from settings (env vars POSTGRES_*).
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    'Offline' mode: emit SQL without connecting. Async drivers don't
    support this directly, so strip the +asyncpg suffix to a plain driver.
    """
    url = config.get_main_option("sqlalchemy.url") or ""
    sync_url = url.replace("postgresql+asyncpg", "postgresql")
    context.configure(
        url=sync_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """'Online' mode: real async connection."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
