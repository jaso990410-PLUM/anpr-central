"""align LED settings with proven F1 protocol

Revision ID: d4a8c26e71b3
Revises: c3f197d54a60
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d4a8c26e71b3"
down_revision: str | None = "c3f197d54a60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_peripheral_behavior_led_brightness", "peripheral_behaviors", type_="check")
    op.drop_constraint("ck_peripheral_behavior_led_scroll_speed", "peripheral_behaviors", type_="check")
    op.execute("UPDATE peripheral_behaviors SET led_brightness = LEAST(led_brightness, 5), led_scroll_speed = LEAST(led_scroll_speed, 5)")
    op.alter_column("peripheral_behaviors", "led_brightness", server_default=sa.text("5"))
    op.create_check_constraint("ck_peripheral_behavior_led_brightness", "peripheral_behaviors", "led_brightness BETWEEN 0 AND 5")
    op.create_check_constraint("ck_peripheral_behavior_led_scroll_speed", "peripheral_behaviors", "led_scroll_speed BETWEEN 0 AND 5")


def downgrade() -> None:
    op.drop_constraint("ck_peripheral_behavior_led_scroll_speed", "peripheral_behaviors", type_="check")
    op.drop_constraint("ck_peripheral_behavior_led_brightness", "peripheral_behaviors", type_="check")
    op.execute("UPDATE peripheral_behaviors SET led_brightness = GREATEST(led_brightness, 1), led_scroll_speed = GREATEST(led_scroll_speed, 1)")
    op.alter_column("peripheral_behaviors", "led_brightness", server_default=sa.text("7"))
    op.create_check_constraint("ck_peripheral_behavior_led_brightness", "peripheral_behaviors", "led_brightness BETWEEN 1 AND 10")
    op.create_check_constraint("ck_peripheral_behavior_led_scroll_speed", "peripheral_behaviors", "led_scroll_speed BETWEEN 1 AND 10")
