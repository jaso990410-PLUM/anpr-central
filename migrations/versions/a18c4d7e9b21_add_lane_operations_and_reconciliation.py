"""add lane operations and reconciliation

Revision ID: a18c4d7e9b21
Revises: f4c8d1e2a930
Create Date: 2026-10-12
"""

from collections.abc import Sequence
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "a18c4d7e9b21"
down_revision: str | None = "f4c8d1e2a930"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recognition_plate_corrections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recognition_event_id", sa.Uuid(), nullable=False),
        sa.Column("corrected_plate", sa.String(32), nullable=False),
        sa.Column("corrected_plate_normalized", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid()),
        sa.Column("supersedes_correction_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["recognition_event_id"], ["recognition_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["supersedes_correction_id"], ["recognition_plate_corrections.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_plate_corrections_event_created", "recognition_plate_corrections", ["recognition_event_id", "created_at"])

    op.create_table(
        "parking_reconciliations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("action_token", sa.String(64), nullable=False),
        sa.Column("primary_recognition_event_id", sa.Uuid()),
        sa.Column("secondary_recognition_event_id", sa.Uuid()),
        sa.Column("parking_session_id", sa.Uuid()),
        sa.Column("parking_review_item_id", sa.Uuid()),
        sa.Column("previous_state", sa.JSON(), nullable=False),
        sa.Column("resulting_state", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("performed_by_user_id", sa.Uuid()),
        sa.Column("performed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["primary_recognition_event_id"], ["recognition_events.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["secondary_recognition_event_id"], ["recognition_events.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parking_session_id"], ["parking_sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parking_review_item_id"], ["parking_review_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["performed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("action_type IN ('LINK_EXIT_TO_OPEN_SESSION','CREATE_OPEN_SESSION_FROM_ENTRY','CREATE_CLOSED_SESSION_FROM_ENTRY_EXIT_PAIR','MARK_DUPLICATE_ENTRY','ATTACH_RECOVERED_EVENT_TO_SESSION','RESOLVE_WITHOUT_MATCH')", name="ck_parking_reconciliations_action"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("action_token", name="uq_parking_reconciliations_action_token"),
    )
    op.create_index("ix_parking_reconciliations_session", "parking_reconciliations", ["parking_session_id"])
    op.create_index("ix_parking_reconciliations_primary_event", "parking_reconciliations", ["primary_recognition_event_id"])
    op.create_index("ix_parking_reconciliations_secondary_event", "parking_reconciliations", ["secondary_recognition_event_id"])
    op.create_index("ix_parking_reconciliations_performed", "parking_reconciliations", ["performed_at"])

    op.create_table(
        "vehicle_watches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("plate_normalized", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(24), server_default="WATCH", nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("cleared_by_user_id", sa.Uuid()),
        sa.Column("cleared_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["cleared_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("severity IN ('WATCH','HIGH_ATTENTION')", name="ck_vehicle_watches_severity"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vehicle_watches_site_plate", "vehicle_watches", ["site_id", "plate_normalized"])
    op.create_index("ix_vehicle_watches_active", "vehicle_watches", ["active"])
    op.create_index("uq_vehicle_watches_active_site_plate", "vehicle_watches", ["site_id", "plate_normalized"], unique=True, postgresql_where=sa.text("active = true"))

    permissions = sa.table("permissions", sa.column("id", sa.Uuid()), sa.column("code", sa.String()), sa.column("description", sa.String()))
    codes = {
        "operations.view": "View the lane operations console",
        "recognition.correct": "Correct recognition plates while preserving camera evidence",
        "parking.reconcile": "Reconcile parking sessions and exceptions",
        "vehicle.watch": "Manage site-scoped vehicle watch flags",
    }
    op.bulk_insert(permissions, [{"id": uuid.uuid4(), "code": code, "description": description} for code, description in codes.items()])
    op.execute(sa.text("""INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
        WHERE r.code = 'administrator' AND p.code IN ('operations.view','recognition.correct','parking.reconcile','vehicle.watch')
        ON CONFLICT DO NOTHING"""))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN ('operations.view','recognition.correct','parking.reconcile','vehicle.watch'))"))
    op.execute(sa.text("DELETE FROM permissions WHERE code IN ('operations.view','recognition.correct','parking.reconcile','vehicle.watch')"))
    op.drop_index("uq_vehicle_watches_active_site_plate", table_name="vehicle_watches")
    op.drop_index("ix_vehicle_watches_active", table_name="vehicle_watches")
    op.drop_index("ix_vehicle_watches_site_plate", table_name="vehicle_watches")
    op.drop_table("vehicle_watches")
    op.drop_index("ix_parking_reconciliations_performed", table_name="parking_reconciliations")
    op.drop_index("ix_parking_reconciliations_secondary_event", table_name="parking_reconciliations")
    op.drop_index("ix_parking_reconciliations_primary_event", table_name="parking_reconciliations")
    op.drop_index("ix_parking_reconciliations_session", table_name="parking_reconciliations")
    op.drop_table("parking_reconciliations")
    op.drop_index("ix_plate_corrections_event_created", table_name="recognition_plate_corrections")
    op.drop_table("recognition_plate_corrections")
