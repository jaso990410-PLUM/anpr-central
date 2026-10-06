"""add central access list management

Revision ID: f6d2c8a4103e
Revises: e3b7a1c94f20
Create Date: 2026-10-04 23:30:00.000000
"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "f6d2c8a4103e"
down_revision: Union[str, Sequence[str], None] = "e3b7a1c94f20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column("access_list_sync_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_table(
        "vehicles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plate_text", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plate_text"),
    )
    op.create_index("ix_vehicles_plate_text", "vehicles", ["plate_text"])
    op.create_table(
        "access_list_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vehicle_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("list_type", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True)),
        sa.Column("valid_until", sa.DateTime(timezone=True)),
        sa.Column("daily_window_enabled", sa.Boolean(), nullable=False),
        sa.Column("daily_start", sa.Time()),
        sa.Column("daily_end", sa.Time()),
        sa.Column("camera_comment", sa.String(16)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vehicle_id"], ["vehicles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("vehicle_id", "site_id", name="uq_access_list_entry_vehicle_site"),
    )
    for name, columns in (
        ("ix_access_list_entries_vehicle_id", ["vehicle_id"]),
        ("ix_access_list_entries_site_id", ["site_id"]),
        ("ix_access_list_entries_list_type", ["list_type"]),
        ("ix_access_list_entries_enabled", ["enabled"]),
    ):
        op.create_index(name, "access_list_entries", columns)
    op.create_table(
        "access_list_camera_sync",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("access_list_entry_id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("remote_index", sa.String(64)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("verification_details", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["access_list_entry_id"], ["access_list_entries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("access_list_entry_id", "camera_id", name="uq_access_list_camera_sync_entry_camera"),
    )
    for name, columns in (
        ("ix_access_list_camera_sync_access_list_entry_id", ["access_list_entry_id"]),
        ("ix_access_list_camera_sync_camera_id", ["camera_id"]),
        ("ix_access_list_camera_sync_status", ["status"]),
        ("ix_access_list_camera_sync_last_verified", ["last_verified_at"]),
    ):
        op.create_index(name, "access_list_camera_sync", columns)

    permission_table = sa.table("permissions", sa.column("id", sa.Uuid()), sa.column("code", sa.String()), sa.column("description", sa.String()))
    permission_ids = {"access_list.view": uuid.uuid4(), "access_list.manage": uuid.uuid4()}
    op.bulk_insert(permission_table, [
        {"id": permission_ids["access_list.view"], "code": "access_list.view", "description": "View central access-list records and synchronization state"},
        {"id": permission_ids["access_list.manage"], "code": "access_list.manage", "description": "Create, edit, enable, and synchronize access-list records"},
    ])
    connection = op.get_bind()
    administrator_id = connection.execute(sa.text("SELECT id FROM roles WHERE code = 'administrator'")).scalar()
    if administrator_id is not None:
        role_permissions = sa.table("role_permissions", sa.column("role_id", sa.Uuid()), sa.column("permission_id", sa.Uuid()))
        op.bulk_insert(role_permissions, [{"role_id": administrator_id, "permission_id": value} for value in permission_ids.values()])


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN ('access_list.view', 'access_list.manage'))"))
    op.execute(sa.text("DELETE FROM permissions WHERE code IN ('access_list.view', 'access_list.manage')"))
    op.drop_table("access_list_camera_sync")
    op.drop_table("access_list_entries")
    op.drop_table("vehicles")
    op.drop_column("cameras", "access_list_sync_enabled")
