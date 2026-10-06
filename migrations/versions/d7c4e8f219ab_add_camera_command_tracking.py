"""add camera command tracking

Revision ID: d7c4e8f219ab
Revises: c92d781ab304
Create Date: 2026-10-04 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7c4e8f219ab"
down_revision: Union[str, Sequence[str], None] = "c92d781ab304"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "camera_commands",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(length=80), nullable=False),
        sa.Column("command_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("camera_result_code", sa.String(length=32), nullable=True),
        sa.Column("nested_result_code", sa.String(length=32), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("request_metadata", sa.JSON(), nullable=True),
        sa.Column("response_metadata", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["camera_id"], ["cameras.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id"),
    )
    op.create_index(
        op.f("ix_camera_commands_camera_id"),
        "camera_commands",
        ["camera_id"],
        unique=False,
    )
    op.create_index(
        "ix_camera_commands_camera_requested",
        "camera_commands",
        ["camera_id", "requested_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_camera_commands_camera_requested", table_name="camera_commands"
    )
    op.drop_index(
        op.f("ix_camera_commands_camera_id"), table_name="camera_commands"
    )
    op.drop_table("camera_commands")
