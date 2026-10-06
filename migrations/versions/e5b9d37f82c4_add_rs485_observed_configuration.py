"""add observed RS-485 configuration

Revision ID: e5b9d37f82c4
Revises: d4a8c26e71b3
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e5b9d37f82c4"
down_revision: str | None = "d4a8c26e71b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("control_boards", sa.Column("observed_serial_channel", sa.Integer()))
    op.add_column("control_boards", sa.Column("observed_baud_rate", sa.Integer()))
    op.add_column("control_boards", sa.Column("observed_data_bits", sa.Integer()))
    op.add_column("control_boards", sa.Column("observed_parity_id", sa.Integer()))
    op.add_column("control_boards", sa.Column("observed_stop_bits", sa.Integer()))
    op.add_column("control_boards", sa.Column("last_communication_read_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("control_boards", "last_communication_read_at")
    op.drop_column("control_boards", "observed_stop_bits")
    op.drop_column("control_boards", "observed_parity_id")
    op.drop_column("control_boards", "observed_data_bits")
    op.drop_column("control_boards", "observed_baud_rate")
    op.drop_column("control_boards", "observed_serial_channel")
