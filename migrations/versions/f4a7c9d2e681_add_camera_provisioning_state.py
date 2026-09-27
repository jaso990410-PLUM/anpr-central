"""add camera provisioning state

Revision ID: f4a7c9d2e681
Revises: b16f81e51757
Create Date: 2026-09-27 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4a7c9d2e681"
down_revision: Union[str, Sequence[str], None] = "b16f81e51757"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column(
            "provisioning_state",
            sa.String(length=32),
            server_default=sa.text("'UNPROVISIONED'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("cameras", "provisioning_state")
