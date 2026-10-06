"""add vehicle reference name

Revision ID: c42a97e8156d
Revises: f6d2c8a4103e
Create Date: 2026-10-05 01:15:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c42a97e8156d"
down_revision: Union[str, Sequence[str], None] = "f6d2c8a4103e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vehicles", sa.Column("reference_name", sa.String(120), nullable=True))


def downgrade() -> None:
    op.drop_column("vehicles", "reference_name")
