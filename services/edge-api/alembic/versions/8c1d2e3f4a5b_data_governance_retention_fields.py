"""data governance retention evidence fields

Revision ID: 8c1d2e3f4a5b
Revises: 7a3c2b1d4e5f
Create Date: 2026-06-17 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "8c1d2e3f4a5b"
down_revision: Union[str, None] = "7a3c2b1d4e5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PG_INDEXES: tuple[str, ...] = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_edge_alarm_events_meta_gin ON alarm_events USING gin (meta jsonb_path_ops)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_edge_ota_operations_params_gin ON ota_operations USING gin (params jsonb_path_ops)",
)

INDEX_NAMES: tuple[str, ...] = (
    "ix_edge_alarm_events_meta_gin",
    "ix_edge_ota_operations_params_gin",
)


def upgrade() -> None:
    op.add_column("control_command_history", sa.Column("previous_value", sa.Text(), nullable=True))
    op.add_column("control_command_history", sa.Column("plc_readback", sa.JSON(), nullable=True))

    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for statement in PG_INDEXES:
                op.execute(statement)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for name in reversed(INDEX_NAMES):
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")

    op.drop_column("control_command_history", "plc_readback")
    op.drop_column("control_command_history", "previous_value")
