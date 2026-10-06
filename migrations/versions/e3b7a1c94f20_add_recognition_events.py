"""add recognition events

Revision ID: e3b7a1c94f20
Revises: a91e6f4c2b70
Create Date: 2026-10-04 22:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e3b7a1c94f20"
down_revision: Union[str, Sequence[str], None] = "a91e6f4c2b70"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recognition_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("vendor_event_id", sa.String(length=160), nullable=True),
        sa.Column("plate_text", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("scene_image_path", sa.String(length=255), nullable=True),
        sa.Column("plate_image_path", sa.String(length=255), nullable=True),
        sa.Column("event_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, columns in (
        ("ix_recognition_events_camera_id", ["camera_id"]),
        ("ix_recognition_events_site_id", ["site_id"]),
        ("ix_recognition_events_captured_at", ["captured_at"]),
        ("ix_recognition_events_received_at", ["received_at"]),
        ("ix_recognition_events_updated_at", ["updated_at"]),
        ("ix_recognition_events_plate_text", ["plate_text"]),
        ("ix_recognition_events_vendor_event_id", ["vendor_event_id"]),
    ):
        op.create_index(name, "recognition_events", columns, unique=False)


def downgrade() -> None:
    op.drop_table("recognition_events")
