"""add camera live status fields

Revision ID: c92d781ab304
Revises: f4a7c9d2e681
Create Date: 2026-09-27 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c92d781ab304"
down_revision: Union[str, Sequence[str], None] = "f4a7c9d2e681"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column("connection_state", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "cameras",
        sa.Column(
            "last_connected_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "cameras",
        sa.Column("agent_version", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cameras", "agent_version")
    op.drop_column("cameras", "last_connected_at")
    op.drop_column("cameras", "connection_state")
