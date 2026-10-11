"""add response profile led settings

Revision ID: e72c9a4f1b60
Revises: b38e1f6a4c72
Create Date: 2026-10-11
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e72c9a4f1b60"
down_revision: str | None = "b38e1f6a4c72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("decision_response_profiles", sa.Column("led_brightness", sa.Integer(), server_default=sa.text("5"), nullable=False))
    op.add_column("decision_response_profiles", sa.Column("led_scroll_speed", sa.Integer(), server_default=sa.text("5"), nullable=False))
    op.create_check_constraint("ck_decision_response_profiles_led_brightness", "decision_response_profiles", "led_brightness BETWEEN 0 AND 5")
    op.create_check_constraint("ck_decision_response_profiles_led_scroll_speed", "decision_response_profiles", "led_scroll_speed BETWEEN 0 AND 5")


def downgrade() -> None:
    op.drop_constraint("ck_decision_response_profiles_led_scroll_speed", "decision_response_profiles", type_="check")
    op.drop_constraint("ck_decision_response_profiles_led_brightness", "decision_response_profiles", type_="check")
    op.drop_column("decision_response_profiles", "led_scroll_speed")
    op.drop_column("decision_response_profiles", "led_brightness")
