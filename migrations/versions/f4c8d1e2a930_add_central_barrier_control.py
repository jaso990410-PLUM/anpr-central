"""add central barrier control

Revision ID: f4c8d1e2a930
Revises: e72c9a4f1b60
Create Date: 2026-10-11
"""

from collections.abc import Sequence
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "f4c8d1e2a930"
down_revision: str | None = "e72c9a4f1b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "barrier_configurations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("control_mode", sa.String(24), server_default="DISABLED", nullable=False),
        sa.Column("adapter_type", sa.String(40), server_default="VISIONZENITH_DIRECT_OPEN", nullable=False),
        sa.Column("feedback_mode", sa.String(24), server_default="NONE", nullable=False),
        sa.Column("commissioned_at", sa.DateTime(timezone=True)),
        sa.Column("commissioned_by_user_id", sa.Uuid()),
        sa.Column("local_auto_open_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("local_auto_open_verified_at", sa.DateTime(timezone=True)),
        sa.Column("local_auto_open_verified_by_user_id", sa.Uuid()),
        sa.Column("commissioning_notes", sa.Text()),
        sa.Column("open_confirmation_timeout_seconds", sa.Integer(), server_default="10", nullable=False),
        sa.Column("open_status_input", sa.Integer()),
        sa.Column("open_active_level", sa.String(4), server_default="HIGH", nullable=False),
        sa.Column("closed_status_input", sa.Integer()),
        sa.Column("closed_active_level", sa.String(4), server_default="HIGH", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["commissioned_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["local_auto_open_verified_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("control_mode IN ('DISABLED','CAMERA_LOCAL','CENTRAL_MANAGED')", name="ck_barrier_configurations_control_mode"),
        sa.CheckConstraint("adapter_type = 'VISIONZENITH_DIRECT_OPEN'", name="ck_barrier_configurations_adapter_type"),
        sa.CheckConstraint("feedback_mode IN ('NONE','NATIVE_GATE_STATUS','GPIO_INPUTS')", name="ck_barrier_configurations_feedback_mode"),
        sa.CheckConstraint("open_active_level IN ('HIGH','LOW')", name="ck_barrier_configurations_open_active_level"),
        sa.CheckConstraint("closed_active_level IN ('HIGH','LOW')", name="ck_barrier_configurations_closed_active_level"),
        sa.CheckConstraint("open_confirmation_timeout_seconds BETWEEN 1 AND 60", name="ck_barrier_configurations_confirmation_timeout"),
        sa.CheckConstraint("open_status_input IS NULL OR open_status_input BETWEEN 0 AND 255", name="ck_barrier_configurations_open_input"),
        sa.CheckConstraint("closed_status_input IS NULL OR closed_status_input BETWEEN 0 AND 255", name="ck_barrier_configurations_closed_input"),
        sa.CheckConstraint("open_status_input IS NULL OR closed_status_input IS NULL OR open_status_input <> closed_status_input", name="ck_barrier_configurations_distinct_inputs"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("camera_id", name="uq_barrier_configurations_camera_id"),
    )
    op.create_table(
        "barrier_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_state", sa.String(24), server_default="UNKNOWN", nullable=False),
        sa.Column("feedback_source", sa.String(24), server_default="NONE", nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("last_known_state", sa.String(24)),
        sa.Column("last_known_at", sa.DateTime(timezone=True)),
        sa.Column("connect_status", sa.Integer()),
        sa.Column("gate_enabled", sa.Boolean()),
        sa.Column("gate_ctrl_id", sa.String(100)),
        sa.Column("open_input_raw", sa.Integer()),
        sa.Column("closed_input_raw", sa.Integer()),
        sa.Column("metadata_json", sa.JSON()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.CheckConstraint("normalized_state IN ('UNKNOWN','CLOSED','OPEN','INTERMEDIATE','INVALID_FEEDBACK')", name="ck_barrier_states_normalized_state"),
        sa.CheckConstraint("feedback_source IN ('NONE','NATIVE_GATE_STATUS','GPIO_INPUTS')", name="ck_barrier_states_feedback_source"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("camera_id", name="uq_barrier_states_camera_id"),
    )
    op.create_table(
        "barrier_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("recognition_event_id", sa.Uuid()),
        sa.Column("access_decision_id", sa.Uuid()),
        sa.Column("parking_review_item_id", sa.Uuid()),
        sa.Column("action_type", sa.String(16), server_default="OPEN", nullable=False),
        sa.Column("trigger_source", sa.String(24), nullable=False),
        sa.Column("command_status", sa.String(32), server_default="QUEUED", nullable=False),
        sa.Column("request_id", sa.String(80)),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("camera_ack_at", sa.DateTime(timezone=True)),
        sa.Column("physical_confirmation_status", sa.String(32), server_default="NOT_AVAILABLE", nullable=False),
        sa.Column("physical_confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("requested_by_user_id", sa.Uuid()),
        sa.Column("operator_reason", sa.String(500)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recognition_event_id"], ["recognition_events.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["access_decision_id"], ["access_decisions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parking_review_item_id"], ["parking_review_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("action_type = 'OPEN'", name="ck_barrier_actions_action_type"),
        sa.CheckConstraint("trigger_source IN ('AUTO_DECISION','MANUAL_RELEASE','COMMISSIONING_TEST')", name="ck_barrier_actions_trigger_source"),
        sa.CheckConstraint("command_status IN ('QUEUED','PUBLISHED','CAMERA_ACKNOWLEDGED','CAMERA_REJECTED','PUBLISH_FAILED','TIMEOUT_UNCERTAIN','EXPIRED_NOT_SENT','SUPPRESSED_ALREADY_OPEN','NOT_CONFIGURED')", name="ck_barrier_actions_command_status"),
        sa.CheckConstraint("physical_confirmation_status IN ('NOT_AVAILABLE','PENDING','CONFIRMED_OPEN','FEEDBACK_NOT_OBSERVED')", name="ck_barrier_actions_physical_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id"),
    )
    op.create_index("ix_barrier_actions_access_decision_id", "barrier_actions", ["access_decision_id"])
    op.create_index("ix_barrier_actions_camera_requested", "barrier_actions", ["camera_id", "requested_at"])
    op.create_index("ix_barrier_actions_command_status", "barrier_actions", ["command_status"])
    op.create_index("ix_barrier_actions_trigger_requested", "barrier_actions", ["trigger_source", "requested_at"])
    op.create_index("uq_barrier_actions_auto_decision", "barrier_actions", ["access_decision_id"], unique=True, postgresql_where=sa.text("trigger_source = 'AUTO_DECISION'"))

    permissions = sa.table("permissions", sa.column("id", sa.Uuid()), sa.column("code", sa.String()), sa.column("description", sa.String()))
    permission_rows = [
        {"id": uuid.uuid4(), "code": "barrier.manage", "description": "Configure and commission barrier integrations"},
        {"id": uuid.uuid4(), "code": "barrier.operate", "description": "Operate and test physical barriers"},
    ]
    op.bulk_insert(permissions, permission_rows)
    op.execute(sa.text("""INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
        WHERE r.code = 'administrator' AND p.code IN ('barrier.manage','barrier.operate')
        ON CONFLICT DO NOTHING"""))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN ('barrier.manage','barrier.operate'))"))
    op.execute(sa.text("DELETE FROM permissions WHERE code IN ('barrier.manage','barrier.operate')"))
    op.drop_index("uq_barrier_actions_auto_decision", table_name="barrier_actions")
    op.drop_index("ix_barrier_actions_trigger_requested", table_name="barrier_actions")
    op.drop_index("ix_barrier_actions_command_status", table_name="barrier_actions")
    op.drop_index("ix_barrier_actions_camera_requested", table_name="barrier_actions")
    op.drop_index("ix_barrier_actions_access_decision_id", table_name="barrier_actions")
    op.drop_table("barrier_actions")
    op.drop_table("barrier_states")
    op.drop_table("barrier_configurations")
