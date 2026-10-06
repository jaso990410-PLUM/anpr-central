"""add offline recognition recovery

Revision ID: f91c0e72a6d4
Revises: e5b9d37f82c4
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "f91c0e72a6d4"
down_revision: str | None = "e5b9d37f82c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("recognition_events", sa.Column("vendor_record_id", sa.String(80)))
    op.add_column("recognition_events", sa.Column("source", sa.String(32), server_default="LIVE", nullable=False))
    op.add_column("recognition_events", sa.Column("recovered_at", sa.DateTime(timezone=True)))
    op.create_index("ix_recognition_events_source", "recognition_events", ["source"])
    op.create_index("ix_recognition_events_camera_captured", "recognition_events", ["camera_id", "captured_at"])
    op.create_table(
        "offline_recovery_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("initial_reported_count", sa.Integer(), nullable=False),
        sa.Column("last_reported_count", sa.Integer(), nullable=False),
        sa.Column("requested_max_count", sa.Integer(), nullable=False),
        sa.Column("requested_min_id", sa.Integer(), nullable=False),
        sa.Column("messages_received", sa.Integer(), server_default="0", nullable=False),
        sa.Column("events_inserted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duplicates_skipped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("invalid_skipped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("batch_start_messages", sa.Integer(), server_default="0", nullable=False),
        sa.Column("batch_start_inserted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("command_request_id", sa.String(80)),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_offline_recovery_runs_camera_id", "offline_recovery_runs", ["camera_id"])
    op.create_index("ix_offline_recovery_camera_started", "offline_recovery_runs", ["camera_id", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_offline_recovery_camera_started", table_name="offline_recovery_runs")
    op.drop_index("ix_offline_recovery_runs_camera_id", table_name="offline_recovery_runs")
    op.drop_table("offline_recovery_runs")
    op.drop_index("ix_recognition_events_camera_captured", table_name="recognition_events")
    op.drop_index("ix_recognition_events_source", table_name="recognition_events")
    op.drop_column("recognition_events", "recovered_at")
    op.drop_column("recognition_events", "source")
    op.drop_column("recognition_events", "vendor_record_id")
