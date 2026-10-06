"""add LED preview display settings

Revision ID: c3f197d54a60
Revises: b74d13e5a902
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c3f197d54a60"
down_revision: str | None = "b74d13e5a902"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("peripheral_behaviors", sa.Column("led_brightness", sa.Integer(), server_default=sa.text("7"), nullable=False))
    op.add_column("peripheral_behaviors", sa.Column("led_scroll_speed", sa.Integer(), server_default=sa.text("5"), nullable=False))
    op.create_check_constraint("ck_peripheral_behavior_led_brightness", "peripheral_behaviors", "led_brightness BETWEEN 1 AND 10")
    op.create_check_constraint("ck_peripheral_behavior_led_scroll_speed", "peripheral_behaviors", "led_scroll_speed BETWEEN 1 AND 10")


def downgrade() -> None:
    op.drop_constraint("ck_peripheral_behavior_led_scroll_speed", "peripheral_behaviors", type_="check")
    op.drop_constraint("ck_peripheral_behavior_led_brightness", "peripheral_behaviors", type_="check")
    op.drop_column("peripheral_behaviors", "led_scroll_speed")
    op.drop_column("peripheral_behaviors", "led_brightness")
