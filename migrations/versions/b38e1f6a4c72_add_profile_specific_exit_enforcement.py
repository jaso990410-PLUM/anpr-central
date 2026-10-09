"""add profile specific exit enforcement

Revision ID: b38e1f6a4c72
Revises: a62f4e8c1d30
Create Date: 2026-10-09
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b38e1f6a4c72"
down_revision: str | None = "a62f4e8c1d30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICIES = "'USE_SITE_POLICY','ALLOW','REVIEW','DENY'"


def upgrade() -> None:
    for name, default in (
        ("normal_exit_policy", "USE_SITE_POLICY"), ("expired_exit_policy", "USE_SITE_POLICY"),
        ("overstay_exit_policy", "REVIEW"), ("blacklist_exit_policy", "DENY"),
    ):
        op.add_column("access_profiles", sa.Column(name, sa.String(20), server_default=default, nullable=False))
        op.create_check_constraint(f"ck_access_profiles_{name}", "access_profiles", f"{name} IN ({POLICIES})")
    op.add_column("access_profiles", sa.Column("overstay_exit_grace_minutes", sa.Integer(), server_default="0", nullable=False))
    op.create_check_constraint("ck_access_profiles_exit_grace_nonnegative", "access_profiles", "overstay_exit_grace_minutes >= 0")

    op.execute("""UPDATE access_profiles SET
        normal_exit_policy='ALLOW', expired_exit_policy='ALLOW', overstay_exit_policy='ALLOW', blacklist_exit_policy='DENY'
        WHERE lower(name)='resident'""")
    op.execute("""UPDATE access_profiles SET
        normal_exit_policy='ALLOW', expired_exit_policy='ALLOW', overstay_exit_policy='REVIEW', blacklist_exit_policy='DENY'
        WHERE lower(name)='visitor'""")
    op.execute("""UPDATE access_profiles SET
        normal_exit_policy='ALLOW', expired_exit_policy='REVIEW', overstay_exit_policy='REVIEW', blacklist_exit_policy='DENY'
        WHERE lower(name) IN ('contractor','cleaner')""")

    for name, default in (
        ("effective_normal_exit_policy", "USE_SITE_POLICY"),
        ("effective_expired_exit_policy", "USE_SITE_POLICY"),
        ("effective_overstay_exit_policy", "REVIEW"),
        ("effective_blacklist_exit_policy", "DENY"),
    ):
        op.add_column("parking_sessions", sa.Column(name, sa.String(20), server_default=default, nullable=False))
        op.create_check_constraint(f"ck_parking_sessions_{name}", "parking_sessions", f"{name} IN ({POLICIES})")
    op.add_column("parking_sessions", sa.Column("effective_overstay_exit_grace_minutes", sa.Integer(), server_default="0", nullable=False))
    op.create_check_constraint("ck_parking_sessions_exit_grace_nonnegative", "parking_sessions", "effective_overstay_exit_grace_minutes >= 0")
    op.execute("""UPDATE parking_sessions ps SET
        effective_normal_exit_policy=ap.normal_exit_policy,
        effective_expired_exit_policy=ap.expired_exit_policy,
        effective_overstay_exit_policy=ap.overstay_exit_policy,
        effective_blacklist_exit_policy=ap.blacklist_exit_policy,
        effective_overstay_exit_grace_minutes=ap.overstay_exit_grace_minutes
        FROM access_profiles ap WHERE ps.access_profile_id=ap.id""")

    op.add_column("access_decisions", sa.Column("exit_policy_source", sa.String(40)))
    op.add_column("access_decisions", sa.Column("barrier_intent", sa.String(24), server_default="NONE", nullable=False))
    op.add_column("access_decisions", sa.Column("physical_execution", sa.String(24), server_default="NOT_IMPLEMENTED", nullable=False))
    op.create_check_constraint("ck_access_decisions_barrier_intent", "access_decisions", "barrier_intent IN ('OPEN','HOLD','OPERATOR_REVIEW','NONE')")

    op.create_table(
        "parking_review_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("vehicle_id", sa.Uuid()),
        sa.Column("parking_session_id", sa.Uuid()),
        sa.Column("recognition_event_id", sa.Uuid(), nullable=False),
        sa.Column("access_decision_id", sa.Uuid(), nullable=False),
        sa.Column("item_type", sa.String(40), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="OPEN", nullable=False),
        sa.Column("first_triggered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_by_user_id", sa.Uuid()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by_user_id", sa.Uuid()),
        sa.Column("resolution_note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vehicle_id"], ["vehicles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parking_session_id"], ["parking_sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["recognition_event_id"], ["recognition_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["access_decision_id"], ["access_decisions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["acknowledged_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["resolved_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("status IN ('OPEN','ACKNOWLEDGED','RESOLVED')", name="ck_parking_review_items_status"),
        sa.CheckConstraint("attempt_count > 0", name="ck_parking_review_items_attempt_count"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_parking_review_items_site_id", "parking_review_items", ["site_id"])
    op.create_index("ix_parking_review_items_vehicle_id", "parking_review_items", ["vehicle_id"])
    op.create_index("ix_parking_review_items_parking_session_id", "parking_review_items", ["parking_session_id"])
    op.create_index("ix_parking_review_items_site_status", "parking_review_items", ["site_id", "status"])
    op.create_index("ix_parking_review_items_session_type", "parking_review_items", ["parking_session_id", "item_type"])


def downgrade() -> None:
    op.drop_index("ix_parking_review_items_session_type", table_name="parking_review_items")
    op.drop_index("ix_parking_review_items_site_status", table_name="parking_review_items")
    op.drop_index("ix_parking_review_items_parking_session_id", table_name="parking_review_items")
    op.drop_index("ix_parking_review_items_vehicle_id", table_name="parking_review_items")
    op.drop_index("ix_parking_review_items_site_id", table_name="parking_review_items")
    op.drop_table("parking_review_items")
    op.drop_constraint("ck_access_decisions_barrier_intent", "access_decisions", type_="check")
    op.drop_column("access_decisions", "physical_execution")
    op.drop_column("access_decisions", "barrier_intent")
    op.drop_column("access_decisions", "exit_policy_source")
    op.drop_constraint("ck_parking_sessions_exit_grace_nonnegative", "parking_sessions", type_="check")
    op.drop_column("parking_sessions", "effective_overstay_exit_grace_minutes")
    for name in ("effective_blacklist_exit_policy", "effective_overstay_exit_policy", "effective_expired_exit_policy", "effective_normal_exit_policy"):
        op.drop_constraint(f"ck_parking_sessions_{name}", "parking_sessions", type_="check")
        op.drop_column("parking_sessions", name)
    op.drop_constraint("ck_access_profiles_exit_grace_nonnegative", "access_profiles", type_="check")
    op.drop_column("access_profiles", "overstay_exit_grace_minutes")
    for name in ("blacklist_exit_policy", "overstay_exit_policy", "expired_exit_policy", "normal_exit_policy"):
        op.drop_constraint(f"ck_access_profiles_{name}", "access_profiles", type_="check")
        op.drop_column("access_profiles", name)
