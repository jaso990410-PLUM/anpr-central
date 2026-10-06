"""add control board and peripheral presets

Revision ID: b74d13e5a902
Revises: c42a97e8156d
Create Date: 2026-10-05 02:00:00.000000
"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "b74d13e5a902"
down_revision: Union[str, Sequence[str], None] = "c42a97e8156d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "peripheral_presets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "control_boards",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("serial_channel", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("baud_rate", sa.Integer(), server_default=sa.text("9600"), nullable=False),
        sa.Column("data_bits", sa.Integer(), server_default=sa.text("8"), nullable=False),
        sa.Column("parity", sa.String(16), server_default=sa.text("'NONE'"), nullable=False),
        sa.Column("stop_bits", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("slave_id", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("default_preset_id", sa.Uuid()),
        sa.Column("communication_sync_status", sa.String(32), server_default=sa.text("'UNSYNCED'"), nullable=False),
        sa.Column("last_test_at", sa.DateTime(timezone=True)),
        sa.Column("last_test_result", sa.String(64)),
        sa.Column("runtime_status", sa.String(32), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column("last_applied_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("serial_channel IN (0, 1)", name="ck_control_board_serial_channel"),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["default_preset_id"], ["peripheral_presets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("camera_id"),
    )
    op.create_table(
        "peripheral_behaviors",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("preset_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("traffic_state", sa.String(8), server_default=sa.text("'RED'"), nullable=False),
        sa.Column("traffic_green_seconds", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("speaker_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("speaker_volume", sa.Integer(), server_default=sa.text("7"), nullable=False),
        sa.Column("speaker_speed", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("speaker_voice", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("speaker_pitch", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("speaker_message", sa.String(250), server_default=sa.text("'Welcome {plate}'"), nullable=False),
        sa.Column("plate_pause_ms", sa.Integer(), server_default=sa.text("100"), nullable=False),
        sa.CheckConstraint("mode IN ('IDLE', 'BUSY')", name="ck_peripheral_behavior_mode"),
        sa.CheckConstraint("traffic_state IN ('RED', 'GREEN')", name="ck_peripheral_behavior_traffic"),
        sa.ForeignKeyConstraint(["preset_id"], ["peripheral_presets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("preset_id", "mode", name="uq_peripheral_behavior_preset_mode"),
    )
    op.create_table(
        "peripheral_led_rows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("behavior_id", sa.Uuid(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(16), server_default=sa.text("'STANDARD'"), nullable=False),
        sa.Column("standard_source", sa.String(32), server_default=sa.text("'BLANK'"), nullable=False),
        sa.Column("custom_text", sa.String(255)),
        sa.CheckConstraint("row_number BETWEEN 1 AND 4", name="ck_peripheral_led_row_number"),
        sa.CheckConstraint("content_type IN ('STANDARD', 'CUSTOM')", name="ck_peripheral_led_content_type"),
        sa.ForeignKeyConstraint(["behavior_id"], ["peripheral_behaviors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("behavior_id", "row_number", name="uq_peripheral_led_behavior_row"),
    )
    op.create_table(
        "peripheral_busy_periods",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("preset_id", sa.Uuid(), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.CheckConstraint("day_of_week BETWEEN 0 AND 6", name="ck_peripheral_busy_day"),
        sa.ForeignKeyConstraint(["preset_id"], ["peripheral_presets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_peripheral_busy_periods_preset_id", "peripheral_busy_periods", ["preset_id"])
    op.create_table(
        "peripheral_preset_schedules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("control_board_id", sa.Uuid(), nullable=False),
        sa.Column("preset_id", sa.Uuid(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.ForeignKeyConstraint(["control_board_id"], ["control_boards.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["preset_id"], ["peripheral_presets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_peripheral_schedule_board_window", "peripheral_preset_schedules", ["control_board_id", "starts_at", "ends_at"])

    permission_table = sa.table("permissions", sa.column("id", sa.Uuid()), sa.column("code", sa.String()), sa.column("description", sa.String()))
    definitions = {
        "control_board.view": "View control-board configuration and peripheral behavior",
        "control_board.manage": "Configure control boards, presets, and schedules",
        "control_board.test": "Send explicit control-board hardware test commands",
        "access_list.delete_all_camera": "Delete all access-list records from a camera",
    }
    permission_ids = {code: uuid.uuid4() for code in definitions}
    op.bulk_insert(permission_table, [{"id": permission_ids[code], "code": code, "description": description} for code, description in definitions.items()])
    administrator_id = op.get_bind().execute(sa.text("SELECT id FROM roles WHERE code = 'administrator'")).scalar()
    if administrator_id is not None:
        role_permissions = sa.table("role_permissions", sa.column("role_id", sa.Uuid()), sa.column("permission_id", sa.Uuid()))
        op.bulk_insert(role_permissions, [{"role_id": administrator_id, "permission_id": permission_id} for permission_id in permission_ids.values()])


def downgrade() -> None:
    codes = "'control_board.view','control_board.manage','control_board.test','access_list.delete_all_camera'"
    op.execute(sa.text(f"DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN ({codes}))"))
    op.execute(sa.text(f"DELETE FROM permissions WHERE code IN ({codes})"))
    op.drop_index("ix_peripheral_schedule_board_window", table_name="peripheral_preset_schedules")
    op.drop_table("peripheral_preset_schedules")
    op.drop_index("ix_peripheral_busy_periods_preset_id", table_name="peripheral_busy_periods")
    op.drop_table("peripheral_busy_periods")
    op.drop_table("peripheral_led_rows")
    op.drop_table("peripheral_behaviors")
    op.drop_table("control_boards")
    op.drop_table("peripheral_presets")
