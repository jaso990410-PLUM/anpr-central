"""add residential identity and visitor management

Revision ID: c19d5e8f2a41
Revises: a18c4d7e9b21
Create Date: 2026-10-13
"""

from collections.abc import Sequence
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "c19d5e8f2a41"
down_revision: str | None = "a18c4d7e9b21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table("residential_units",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("unit_code", sa.String(60), nullable=False), sa.Column("unit_code_normalized", sa.String(60), nullable=False),
        sa.Column("display_name", sa.String(150)), sa.Column("block", sa.String(80)), sa.Column("floor", sa.String(40)),
        sa.Column("status", sa.String(16), server_default="ACTIVE", nullable=False), sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", "unit_code_normalized", name="uq_residential_units_site_code"),
        sa.CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="ck_residential_units_status"))
    op.create_index("ix_residential_units_site_id", "residential_units", ["site_id"])

    op.create_table("residents",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("display_name", sa.String(150), nullable=False),
        sa.Column("phone", sa.String(40)), sa.Column("email", sa.String(254)),
        sa.Column("status", sa.String(16), server_default="ACTIVE", nullable=False), sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"), sa.CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="ck_residents_status"))

    op.create_table("residential_site_settings",
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("default_resident_access_profile_id", sa.Uuid()), sa.Column("default_visitor_access_profile_id", sa.Uuid()),
        sa.Column("default_visitor_visit_mode", sa.String(20), server_default="SINGLE_VISIT", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["default_resident_access_profile_id"], ["access_profiles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["default_visitor_access_profile_id"], ["access_profiles.id"], ondelete="SET NULL"), sa.PrimaryKeyConstraint("site_id"),
        sa.CheckConstraint("default_visitor_visit_mode IN ('SINGLE_VISIT','MULTI_ENTRY')", name="ck_residential_site_settings_visit_mode"))

    op.create_table("resident_unit_memberships",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("resident_id", sa.Uuid(), nullable=False), sa.Column("unit_id", sa.Uuid(), nullable=False),
        sa.Column("membership_type", sa.String(16), nullable=False), sa.Column("is_primary_contact", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True)), sa.Column("valid_until", sa.DateTime(timezone=True)),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["resident_id"], ["residents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["unit_id"], ["residential_units.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("membership_type IN ('OWNER','TENANT','FAMILY','OCCUPANT','OTHER')", name="ck_resident_memberships_type"))
    op.create_index("ix_resident_unit_memberships_resident_id", "resident_unit_memberships", ["resident_id"])
    op.create_index("ix_resident_unit_memberships_unit_id", "resident_unit_memberships", ["unit_id"])
    op.create_index("ix_resident_memberships_unit_active", "resident_unit_memberships", ["unit_id", "active"])
    op.create_index("uq_resident_memberships_active_pair", "resident_unit_memberships", ["resident_id", "unit_id"], unique=True, postgresql_where=sa.text("active = true"))

    op.create_table("resident_vehicle_assignments",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("access_list_entry_id", sa.Uuid(), nullable=False),
        sa.Column("unit_id", sa.Uuid(), nullable=False), sa.Column("resident_id", sa.Uuid()), sa.Column("label", sa.String(100)),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["access_list_entry_id"], ["access_list_entries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["unit_id"], ["residential_units.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resident_id"], ["residents.id"], ondelete="SET NULL"), sa.PrimaryKeyConstraint("id"))
    op.create_index("ix_resident_vehicle_assignments_access_list_entry_id", "resident_vehicle_assignments", ["access_list_entry_id"])
    op.create_index("ix_resident_vehicle_assignments_unit_id", "resident_vehicle_assignments", ["unit_id"])
    op.create_index("ix_resident_vehicle_assignments_resident_id", "resident_vehicle_assignments", ["resident_id"])
    op.create_index("ix_resident_vehicle_unit_active", "resident_vehicle_assignments", ["unit_id", "active"])
    op.create_index("uq_resident_vehicle_active_entry", "resident_vehicle_assignments", ["access_list_entry_id"], unique=True, postgresql_where=sa.text("active = true"))

    op.create_table("visitor_reservations",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("site_id", sa.Uuid(), nullable=False), sa.Column("unit_id", sa.Uuid(), nullable=False),
        sa.Column("host_resident_id", sa.Uuid()), sa.Column("visitor_name", sa.String(150), nullable=False), sa.Column("visitor_phone", sa.String(40)),
        sa.Column("purpose", sa.String(250)), sa.Column("plate", sa.String(32)), sa.Column("plate_normalized", sa.String(32)),
        sa.Column("access_profile_id", sa.Uuid()), sa.Column("max_stay_minutes_override", sa.Integer()),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False), sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_arrival_at", sa.DateTime(timezone=True)), sa.Column("visit_mode", sa.String(20), nullable=False),
        sa.Column("approval_status", sa.String(16), server_default="PENDING", nullable=False),
        sa.Column("visit_state", sa.String(16), server_default="EXPECTED", nullable=False),
        sa.Column("access_list_entry_id", sa.Uuid()), sa.Column("access_binding_mode", sa.String(24), server_default="NONE", nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid()), sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("approved_by_user_id", sa.Uuid()), sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("rejected_by_user_id", sa.Uuid()), sa.Column("rejected_at", sa.DateTime(timezone=True)), sa.Column("rejection_reason", sa.String(500)),
        sa.Column("cancelled_by_user_id", sa.Uuid()), sa.Column("cancelled_at", sa.DateTime(timezone=True)), sa.Column("cancellation_reason", sa.String(500)),
        sa.Column("first_arrived_at", sa.DateTime(timezone=True)), sa.Column("last_departed_at", sa.DateTime(timezone=True)), sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["unit_id"], ["residential_units.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["host_resident_id"], ["residents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["access_profile_id"], ["access_profiles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["access_list_entry_id"], ["access_list_entries.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["rejected_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["cancelled_by_user_id"], ["users.id"], ondelete="SET NULL"), sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("valid_until > valid_from", name="ck_visitor_reservations_window"),
        sa.CheckConstraint("visit_mode IN ('SINGLE_VISIT','MULTI_ENTRY')", name="ck_visitor_reservations_mode"),
        sa.CheckConstraint("approval_status IN ('PENDING','APPROVED','REJECTED','CANCELLED')", name="ck_visitor_reservations_approval"),
        sa.CheckConstraint("visit_state IN ('EXPECTED','ARRIVED','DEPARTED','COMPLETED','EXPIRED')", name="ck_visitor_reservations_state"),
        sa.CheckConstraint("access_binding_mode IN ('NONE','TEMPORARY_ACCESS','EXISTING_ACCESS')", name="ck_visitor_reservations_binding"))
    op.create_index("ix_visitor_reservations_site_id", "visitor_reservations", ["site_id"])
    op.create_index("ix_visitor_reservations_unit_id", "visitor_reservations", ["unit_id"])
    op.create_index("ix_visitor_reservations_host_resident_id", "visitor_reservations", ["host_resident_id"])
    op.create_index("ix_visitor_reservations_access_list_entry_id", "visitor_reservations", ["access_list_entry_id"])
    op.create_index("ix_visitor_reservations_site_window", "visitor_reservations", ["site_id", "valid_from", "valid_until"])
    op.create_index("ix_visitor_reservations_plate_status", "visitor_reservations", ["site_id", "plate_normalized", "approval_status"])

    op.add_column("parking_sessions", sa.Column("visitor_reservation_id", sa.Uuid()))
    op.create_foreign_key("fk_parking_sessions_visitor_reservation_id", "parking_sessions", "visitor_reservations", ["visitor_reservation_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_parking_sessions_visitor_reservation_id", "parking_sessions", ["visitor_reservation_id"])

    conn = op.get_bind()
    for code, description in {
        "residential.view": "View residential units, residents, and vehicle relationships",
        "residential.manage": "Manage residential identity and vehicle relationships",
        "visitor.manage": "Create and manage visitor reservations",
        "visitor.approve": "Approve and reject visitor reservations",
    }.items():
        conn.execute(
            sa.text("INSERT INTO permissions (id,code,description) SELECT :id,:insert_code,:description WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE code=:lookup_code)"),
            {"id": uuid.uuid4(), "insert_code": code, "lookup_code": code, "description": description},
        )
    conn.execute(sa.text("""INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id,p.id FROM roles r CROSS JOIN permissions p
        WHERE r.code='administrator' AND p.code IN ('residential.view','residential.manage','visitor.manage','visitor.approve')
        AND NOT EXISTS (SELECT 1 FROM role_permissions rp WHERE rp.role_id=r.id AND rp.permission_id=p.id)"""))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN ('residential.view','residential.manage','visitor.manage','visitor.approve'))"))
    op.execute(sa.text("DELETE FROM permissions WHERE code IN ('residential.view','residential.manage','visitor.manage','visitor.approve')"))
    op.drop_index("ix_parking_sessions_visitor_reservation_id", table_name="parking_sessions")
    op.drop_constraint("fk_parking_sessions_visitor_reservation_id", "parking_sessions", type_="foreignkey")
    op.drop_column("parking_sessions", "visitor_reservation_id")
    op.drop_table("visitor_reservations")
    op.drop_table("resident_vehicle_assignments")
    op.drop_table("resident_unit_memberships")
    op.drop_table("residential_site_settings")
    op.drop_table("residents")
    op.drop_table("residential_units")
