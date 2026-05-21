"""add ota operation id

Revision ID: 7a3c2b1d4e5f
Revises: cc4d573ff116
Create Date: 2026-05-22 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "7a3c2b1d4e5f"
down_revision: Union[str, None] = "cc4d573ff116"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ota_operations", sa.Column("operation_id", sa.String(length=64), nullable=True))
    op.create_index("ix_ota_operation_id", "ota_operations", ["operation_id"])


def downgrade() -> None:
    op.drop_index("ix_ota_operation_id", table_name="ota_operations")
    op.drop_column("ota_operations", "operation_id")
