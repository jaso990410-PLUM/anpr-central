"""add residential access decisions and stay management

Revision ID: a62f4e8c1d30
Revises: f91c0e72a6d4
Create Date: 2026-10-08
"""

from collections.abc import Sequence
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "a62f4e8c1d30"
down_revision: str | None = "f91c0e72a6d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cameras", sa.Column("lane_role", sa.String(20), server_default="UNSPECIFIED", nullable=False))
    op.execute("UPDATE cameras SET lane_role = CASE WHEN upper(direction) IN ('ENTRY','EXIT') THEN upper(direction) ELSE 'UNSPECIFIED' END")

    op.create_table(
        "decision_response_profiles",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False), sa.Column("response_kind", sa.String(16), nullable=False),
        sa.Column("led_rows", sa.JSON(), nullable=False), sa.Column("display_seconds", sa.Integer(), nullable=False),
        sa.Column("traffic_state", sa.String(8), nullable=False), sa.Column("traffic_green_seconds", sa.Integer(), nullable=False),
        sa.Column("speaker_enabled", sa.Boolean(), nullable=False), sa.Column("speaker_message", sa.String(250), nullable=False),
        sa.Column("speaker_volume", sa.Integer(), nullable=False), sa.Column("speaker_speed", sa.Integer(), nullable=False),
        sa.Column("speaker_voice", sa.Integer(), nullable=False), sa.Column("speaker_pitch", sa.Integer(), nullable=False),
        sa.Column("plate_pause_ms", sa.Integer(), nullable=False), sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", "name", name="uq_decision_response_profile_site_name"),
    )
    op.create_index("ix_decision_response_profiles_site_id", "decision_response_profiles", ["site_id"])
    op.create_table(
        "access_profiles",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False), sa.Column("description", sa.Text()),
        sa.Column("default_max_stay_minutes", sa.Integer()), sa.Column("overstay_monitoring_enabled", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", "name", name="uq_access_profile_site_name"),
    )
    op.create_index("ix_access_profiles_site_id", "access_profiles", ["site_id"])
    op.create_table(
        "site_policies",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("operating_mode", sa.String(24), nullable=False), sa.Column("unknown_entry_policy", sa.String(16), nullable=False),
        sa.Column("exit_mode", sa.String(16), nullable=False), sa.Column("exit_blacklist_policy", sa.String(16), nullable=False),
        sa.Column("allow_response_profile_id", sa.Uuid()), sa.Column("deny_response_profile_id", sa.Uuid()),
        sa.Column("review_response_profile_id", sa.Uuid()), sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["allow_response_profile_id"], ["decision_response_profiles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["deny_response_profile_id"], ["decision_response_profiles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["review_response_profile_id"], ["decision_response_profiles.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("site_id"),
    )
    op.add_column("access_list_entries", sa.Column("access_profile_id", sa.Uuid()))
    op.add_column("access_list_entries", sa.Column("max_stay_minutes_override", sa.Integer()))
    op.create_foreign_key("fk_access_list_entries_access_profile_id_access_profiles", "access_list_entries", "access_profiles", ["access_profile_id"], ["id"], ondelete="SET NULL")

    op.create_table(
        "access_decisions",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("recognition_event_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False), sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("vehicle_id", sa.Uuid()), sa.Column("access_entry_id", sa.Uuid()),
        sa.Column("access_status", sa.String(32), nullable=False), sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False), sa.Column("policy_type", sa.String(24), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False), sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("response_profile_id", sa.Uuid()), sa.Column("response_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["recognition_event_id"], ["recognition_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vehicle_id"], ["vehicles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["access_entry_id"], ["access_list_entries.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["response_profile_id"], ["decision_response_profiles.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("recognition_event_id"),
    )
    op.create_index("ix_access_decisions_site_id", "access_decisions", ["site_id"])
    op.create_index("ix_access_decisions_camera_id", "access_decisions", ["camera_id"])
    op.create_index("ix_access_decisions_vehicle_id", "access_decisions", ["vehicle_id"])
    op.create_index("ix_access_decisions_site_evaluated", "access_decisions", ["site_id", "evaluated_at"])
    op.create_table(
        "parking_sessions",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("vehicle_id", sa.Uuid(), nullable=False), sa.Column("access_profile_id", sa.Uuid()),
        sa.Column("entry_recognition_id", sa.Uuid(), nullable=False), sa.Column("exit_recognition_id", sa.Uuid()),
        sa.Column("status", sa.String(16), nullable=False), sa.Column("entered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exited_at", sa.DateTime(timezone=True)), sa.Column("max_stay_minutes", sa.Integer()),
        sa.Column("overstay_due_at", sa.DateTime(timezone=True)), sa.Column("overstay_started_at", sa.DateTime(timezone=True)),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vehicle_id"], ["vehicles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["access_profile_id"], ["access_profiles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["entry_recognition_id"], ["recognition_events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["exit_recognition_id"], ["recognition_events.id"], ondelete="RESTRICT"), sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_parking_sessions_site_id", "parking_sessions", ["site_id"])
    op.create_index("ix_parking_sessions_vehicle_id", "parking_sessions", ["vehicle_id"])
    op.create_index("ix_parking_sessions_site_status", "parking_sessions", ["site_id", "status"])
    op.create_index("uq_parking_sessions_active_vehicle_site", "parking_sessions", ["site_id", "vehicle_id"], unique=True, postgresql_where=sa.text("status IN ('OPEN', 'OVERSTAY')"))
    op.create_table(
        "parking_session_issues",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("vehicle_id", sa.Uuid()), sa.Column("recognition_event_id", sa.Uuid(), nullable=False),
        sa.Column("issue_type", sa.String(40), nullable=False), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("details", sa.Text()), sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vehicle_id"], ["vehicles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["recognition_event_id"], ["recognition_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("recognition_event_id"),
    )
    op.create_index("ix_parking_session_issues_site_id", "parking_session_issues", ["site_id"])
    op.create_index("ix_parking_session_issues_vehicle_id", "parking_session_issues", ["vehicle_id"])
    op.create_table(
        "overstay_alerts",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("parking_session_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False), sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)), sa.Column("acknowledged_by_user_id", sa.Uuid()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["parking_session_id"], ["parking_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["acknowledged_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("parking_session_id"),
    )

    conn = op.get_bind()
    sites = conn.execute(sa.text("SELECT id FROM sites")).all()
    for (site_id,) in sites:
        profile_ids = {kind: uuid.uuid4() for kind in ("ALLOW", "DENY", "REVIEW")}
        defaults = {
            "ALLOW": ("Access Granted", "GREEN", [{"type": "STANDARD", "source": "PLATE"}, {"type": "CUSTOM", "text": "ACCESS GRANTED"}]),
            "DENY": ("Access Denied", "RED", [{"type": "STANDARD", "source": "PLATE"}, {"type": "CUSTOM", "text": "ACCESS DENIED"}]),
            "REVIEW": ("Manual Review", "RED", [{"type": "STANDARD", "source": "PLATE"}, {"type": "CUSTOM", "text": "PLEASE WAIT"}]),
        }
        for kind, (name, traffic, rows) in defaults.items():
            conn.execute(sa.text("""INSERT INTO decision_response_profiles
                (id,site_id,name,response_kind,led_rows,display_seconds,traffic_state,traffic_green_seconds,speaker_enabled,speaker_message,speaker_volume,speaker_speed,speaker_voice,speaker_pitch,plate_pause_ms,enabled)
                VALUES (:id,:site,:name,:kind,:rows,5,:traffic,5,false,:message,7,5,3,5,100,true)"""),
                {"id": profile_ids[kind], "site": site_id, "name": name, "kind": kind, "rows": sa.text("'[]'") if False else __import__('json').dumps(rows), "traffic": traffic, "message": "Welcome {plate}" if kind == "ALLOW" else "Access denied"})
        conn.execute(sa.text("""INSERT INTO site_policies
            (id,site_id,operating_mode,unknown_entry_policy,exit_mode,exit_blacklist_policy,allow_response_profile_id,deny_response_profile_id,review_response_profile_id,policy_version)
            VALUES (:id,:site,'RESIDENTIAL','DENY','FREE_EXIT','DENY',:allow_id,:deny_id,:review_id,1)"""),
            {"id": uuid.uuid4(), "site": site_id, "allow_id": profile_ids["ALLOW"], "deny_id": profile_ids["DENY"], "review_id": profile_ids["REVIEW"]})
        conn.execute(sa.text("""INSERT INTO access_profiles
            (id,site_id,name,description,default_max_stay_minutes,overstay_monitoring_enabled,enabled)
            VALUES (:id,:site,'Resident','Default residential access',NULL,true,true)"""), {"id": uuid.uuid4(), "site": site_id})

    conn.execute(sa.text("INSERT INTO permissions (id,code,description) SELECT :id,'parking.view','View parking sessions, decisions, and alerts' WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE code='parking.view')"), {"id": uuid.uuid4()})
    conn.execute(sa.text("INSERT INTO permissions (id,code,description) SELECT :id,'parking.manage','Manage parking policy, profiles, and alerts' WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE code='parking.manage')"), {"id": uuid.uuid4()})
    conn.execute(sa.text("""INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id,p.id FROM roles r CROSS JOIN permissions p
        WHERE r.code='administrator' AND p.code IN ('parking.view','parking.manage')
        AND NOT EXISTS (SELECT 1 FROM role_permissions rp WHERE rp.role_id=r.id AND rp.permission_id=p.id)"""))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN ('parking.view','parking.manage'))"))
    conn.execute(sa.text("DELETE FROM permissions WHERE code IN ('parking.view','parking.manage')"))
    op.drop_table("overstay_alerts")
    op.drop_index("ix_parking_session_issues_vehicle_id", table_name="parking_session_issues")
    op.drop_index("ix_parking_session_issues_site_id", table_name="parking_session_issues")
    op.drop_table("parking_session_issues")
    op.drop_index("uq_parking_sessions_active_vehicle_site", table_name="parking_sessions")
    op.drop_index("ix_parking_sessions_site_status", table_name="parking_sessions")
    op.drop_index("ix_parking_sessions_vehicle_id", table_name="parking_sessions")
    op.drop_index("ix_parking_sessions_site_id", table_name="parking_sessions")
    op.drop_table("parking_sessions")
    op.drop_index("ix_access_decisions_site_evaluated", table_name="access_decisions")
    op.drop_index("ix_access_decisions_vehicle_id", table_name="access_decisions")
    op.drop_index("ix_access_decisions_camera_id", table_name="access_decisions")
    op.drop_index("ix_access_decisions_site_id", table_name="access_decisions")
    op.drop_table("access_decisions")
    op.drop_constraint("fk_access_list_entries_access_profile_id_access_profiles", "access_list_entries", type_="foreignkey")
    op.drop_column("access_list_entries", "max_stay_minutes_override")
    op.drop_column("access_list_entries", "access_profile_id")
    op.drop_table("site_policies")
    op.drop_index("ix_access_profiles_site_id", table_name="access_profiles")
    op.drop_table("access_profiles")
    op.drop_index("ix_decision_response_profiles_site_id", table_name="decision_response_profiles")
    op.drop_table("decision_response_profiles")
    op.drop_column("cameras", "lane_role")
