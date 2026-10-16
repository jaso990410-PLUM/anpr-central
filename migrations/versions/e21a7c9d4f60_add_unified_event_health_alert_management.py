"""add unified event health alert management

Revision ID: e21a7c9d4f60
Revises: d20a6f3b8c52
Create Date: 2026-10-16
"""

from collections.abc import Sequence
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "e21a7c9d4f60"
down_revision: str | None = "d20a6f3b8c52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "unified_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("attention_required", sa.Boolean(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("site_id", sa.Uuid(), sa.ForeignKey("sites.id", ondelete="SET NULL")),
        sa.Column("lane_id", sa.Uuid()),
        sa.Column("camera_id", sa.Uuid(), sa.ForeignKey("cameras.id", ondelete="SET NULL")),
        sa.Column("source_type", sa.String(80), nullable=False),
        sa.Column("source_id", sa.String(160), nullable=False),
        sa.Column("correlation_id", sa.Uuid()),
        sa.Column("display_subject", sa.String(180), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("audit_significant", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("category IN ('TRAFFIC_ANPR','ACCESS_SECURITY','ADMIN_BUSINESS','OPERATOR_ACTION','SYSTEM_HEALTH')", name="category"),
        sa.CheckConstraint("severity IN ('INFO','WARNING','CRITICAL')", name="severity"),
        sa.UniqueConstraint("source_type", "source_id", "event_type", name="uq_unified_events_source_event"),
    )
    op.create_index("ix_unified_events_occurred_at", "unified_events", ["occurred_at"])
    op.create_index("ix_unified_events_site_occurred", "unified_events", ["site_id", "occurred_at"])
    op.create_index("ix_unified_events_category_occurred", "unified_events", ["category", "occurred_at"])
    op.create_index("ix_unified_events_attention_occurred", "unified_events", ["attention_required", "occurred_at"])
    op.create_index("ix_unified_events_source", "unified_events", ["source_type", "source_id"])
    op.create_index("ix_unified_events_correlation", "unified_events", ["correlation_id"])

    op.create_table(
        "operational_alerts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("alert_type", sa.String(80), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("site_id", sa.Uuid(), sa.ForeignKey("sites.id", ondelete="SET NULL")),
        sa.Column("component_type", sa.String(60), nullable=False),
        sa.Column("component_id", sa.String(160), nullable=False),
        sa.Column("fingerprint", sa.String(320), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_by_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("acknowledgement_note", sa.Text()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution_reason", sa.String(500)),
        sa.Column("trigger_event_id", sa.Uuid(), sa.ForeignKey("unified_events.id", ondelete="SET NULL")),
        sa.Column("resolution_event_id", sa.Uuid(), sa.ForeignKey("unified_events.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("severity IN ('WARNING','CRITICAL')", name="severity"),
        sa.CheckConstraint("status IN ('ACTIVE','ACKNOWLEDGED','RESOLVED')", name="status"),
        sa.UniqueConstraint("fingerprint", name="uq_operational_alerts_fingerprint"),
    )
    op.create_index("ix_operational_alerts_status", "operational_alerts", ["status"])
    op.create_index("ix_operational_alerts_severity", "operational_alerts", ["severity"])
    op.create_index("ix_operational_alerts_site_status", "operational_alerts", ["site_id", "status"])

    op.create_table(
        "component_health",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("component_type", sa.String(60), nullable=False),
        sa.Column("component_id", sa.String(160), nullable=False),
        sa.Column("site_id", sa.Uuid(), sa.ForeignKey("sites.id", ondelete="SET NULL")),
        sa.Column("display_name", sa.String(180), nullable=False),
        sa.Column("health_state", sa.String(16), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("capabilities", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("health_state IN ('HEALTHY','DEGRADED','FAULT','UNKNOWN','UNVERIFIED','DISABLED')", name="state"),
        sa.UniqueConstraint("component_type", "component_id", name="uq_component_health_identity"),
    )
    op.create_index("ix_component_health_site_state", "component_health", ["site_id", "health_state"])
    op.create_index("ix_component_health_observed", "component_health", ["observed_at"])

    connection = op.get_bind()
    permission_rows = {
        "monitoring.view": "View operational events, alerts, and system health",
        "alerts.acknowledge": "Acknowledge active operational alerts",
        "monitoring.manage": "Manage monitoring configuration",
    }
    permission_insert = sa.text(
        "INSERT INTO permissions (id,code,description) "
        "SELECT :id,:code,:description "
        "WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE code=:code)"
    ).bindparams(
        sa.bindparam("id", type_=sa.Uuid()),
        sa.bindparam("code", type_=sa.String(100)),
        sa.bindparam("description", type_=sa.String(255)),
    )
    for code, description in permission_rows.items():
        connection.execute(permission_insert, {"id": uuid.uuid4(), "code": code, "description": description})
    connection.execute(sa.text("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
        WHERE ((r.code = 'administrator' AND p.code IN ('monitoring.view','alerts.acknowledge','monitoring.manage'))
           OR (r.code IN ('operator','security') AND p.code IN ('monitoring.view','alerts.acknowledge'))
           OR (r.code = 'viewer' AND p.code = 'monitoring.view'))
        AND NOT EXISTS (SELECT 1 FROM role_permissions rp WHERE rp.role_id=r.id AND rp.permission_id=p.id)
    """))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN ('monitoring.view','alerts.acknowledge','monitoring.manage'))"))
    op.execute(sa.text("DELETE FROM permissions WHERE code IN ('monitoring.view','alerts.acknowledge','monitoring.manage')"))
    op.drop_index("ix_component_health_observed", table_name="component_health")
    op.drop_index("ix_component_health_site_state", table_name="component_health")
    op.drop_table("component_health")
    op.drop_index("ix_operational_alerts_site_status", table_name="operational_alerts")
    op.drop_index("ix_operational_alerts_severity", table_name="operational_alerts")
    op.drop_index("ix_operational_alerts_status", table_name="operational_alerts")
    op.drop_table("operational_alerts")
    op.drop_index("ix_unified_events_correlation", table_name="unified_events")
    op.drop_index("ix_unified_events_source", table_name="unified_events")
    op.drop_index("ix_unified_events_attention_occurred", table_name="unified_events")
    op.drop_index("ix_unified_events_category_occurred", table_name="unified_events")
    op.drop_index("ix_unified_events_site_occurred", table_name="unified_events")
    op.drop_index("ix_unified_events_occurred_at", table_name="unified_events")
    op.drop_table("unified_events")
