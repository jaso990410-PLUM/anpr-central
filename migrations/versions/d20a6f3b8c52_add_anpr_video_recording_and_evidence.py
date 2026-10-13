"""add ANPR video recording and evidence

Revision ID: d20a6f3b8c52
Revises: c19d5e8f2a41
"""
from collections.abc import Sequence
import uuid
from alembic import op
import sqlalchemy as sa

revision: str = "d20a6f3b8c52"
down_revision: str | None = "c19d5e8f2a41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table("media_storages",
        sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("storage_type", sa.String(16), nullable=False), sa.Column("root_path", sa.String(500), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False), sa.Column("retention_days", sa.Integer()),
        sa.Column("minimum_free_space_gb", sa.Integer(), server_default="10", nullable=False),
        sa.Column("minimum_free_space_percent", sa.Integer(), server_default="5", nullable=False),
        sa.Column("last_successful_write_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("storage_type IN ('LOCAL','NETWORK')", name="ck_media_storages_type"))
    op.create_table("media_sources",
        sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("provider_type", sa.String(24), server_default="NONE", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("recording_stream_uri", sa.String(1000)), sa.Column("live_stream_uri", sa.String(1000)),
        sa.Column("credential_reference", sa.String(120)), sa.Column("storage_id", sa.Uuid()),
        sa.Column("recording_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("pre_event_seconds", sa.Integer(), server_default="10", nullable=False),
        sa.Column("post_event_seconds", sa.Integer(), server_default="15", nullable=False),
        sa.Column("segment_duration_seconds", sa.Integer(), server_default="60", nullable=False),
        sa.Column("media_time_offset_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("expected_codec", sa.String(30)), sa.Column("detected_codec", sa.String(30)),
        sa.Column("detected_width", sa.Integer()), sa.Column("detected_height", sa.Integer()),
        sa.Column("approximate_bitrate_kbps", sa.Integer()),
        sa.Column("recorder_state", sa.String(20), server_default="DISABLED", nullable=False),
        sa.Column("last_stream_activity_at", sa.DateTime(timezone=True)), sa.Column("last_successful_segment_at", sa.DateTime(timezone=True)),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False), sa.Column("last_error", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["storage_id"], ["media_storages.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("camera_id"),
        sa.CheckConstraint("provider_type IN ('NONE','CENTRAL_RECORDER','EXTERNAL_NVR')", name="ck_media_sources_provider"),
        sa.CheckConstraint("recorder_state IN ('DISABLED','STARTING','RECORDING','DEGRADED','DISCONNECTED','ERROR')", name="ck_media_sources_state"))
    op.create_index("ix_media_sources_camera_id", "media_sources", ["camera_id"], unique=True)
    op.create_table("recording_segments",
        sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("media_source_id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False), sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)), sa.Column("relative_path", sa.String(700), nullable=False, unique=True),
        sa.Column("state", sa.String(16), nullable=False), sa.Column("codec", sa.String(30)), sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()), sa.Column("file_size_bytes", sa.BigInteger()), sa.Column("duration_ms", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["media_source_id"], ["media_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.CheckConstraint("state IN ('WRITING','COMPLETE','PARTIAL','FAILED','DELETING')", name="ck_recording_segments_state"))
    op.create_index("ix_recording_segments_media_source_id", "recording_segments", ["media_source_id"])
    op.create_index("ix_recording_segments_camera_id", "recording_segments", ["camera_id"])
    op.create_index("ix_recording_segments_camera_started", "recording_segments", ["camera_id", "started_at"])
    op.create_index("ix_recording_segments_camera_ended", "recording_segments", ["camera_id", "ended_at"])
    op.create_index("ix_recording_segments_state_started", "recording_segments", ["state", "started_at"])
    op.create_table("event_media_references",
        sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("recognition_event_id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False), sa.Column("media_source_id", sa.Uuid()),
        sa.Column("media_anchor_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("clip_start_at", sa.DateTime(timezone=True), nullable=False), sa.Column("clip_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(24), nullable=False), sa.Column("provider_reference", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["recognition_event_id"], ["recognition_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["media_source_id"], ["media_sources.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("recognition_event_id"),
        sa.CheckConstraint("status IN ('PENDING','AVAILABLE','PARTIAL','UNAVAILABLE','EXPIRED','RECORDER_OFFLINE','NOT_CONFIGURED')", name="ck_event_media_references_status"))
    op.create_index("ix_event_media_references_recognition_event_id", "event_media_references", ["recognition_event_id"], unique=True)
    op.create_index("ix_event_media_references_camera_id", "event_media_references", ["camera_id"])
    op.create_table("media_evidence_holds",
        sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("recognition_event_id", sa.Uuid()), sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False), sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid()), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("released_by_user_id", sa.Uuid()), sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recognition_event_id"], ["recognition_events.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["released_by_user_id"], ["users.id"], ondelete="SET NULL"))
    op.create_index("ix_media_evidence_holds_camera_id", "media_evidence_holds", ["camera_id"])
    op.create_index("ix_media_evidence_holds_camera_window", "media_evidence_holds", ["camera_id", "start_at", "end_at"])
    op.create_table("media_exports",
        sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("requested_by_user_id", sa.Uuid()),
        sa.Column("camera_id", sa.Uuid(), nullable=False), sa.Column("recognition_event_id", sa.Uuid()),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False), sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), server_default="QUEUED", nullable=False), sa.Column("output_relative_path", sa.String(700)),
        sa.Column("file_size_bytes", sa.BigInteger()), sa.Column("checksum_sha256", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)), sa.Column("error", sa.Text()),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recognition_event_id"], ["recognition_events.id"], ondelete="SET NULL"),
        sa.CheckConstraint("state IN ('QUEUED','PROCESSING','COMPLETED','FAILED','EXPIRED')", name="ck_media_exports_state"))
    op.create_index("ix_media_exports_requested_by_user_id", "media_exports", ["requested_by_user_id"])
    op.create_index("ix_media_exports_camera_id", "media_exports", ["camera_id"])
    conn = op.get_bind()
    for code, description in {"media.view":"View ANPR recording and playback", "media.export":"Export and download ANPR evidence", "media.manage":"Configure ANPR recording, storage, and evidence holds"}.items():
        conn.execute(sa.text("INSERT INTO permissions (id,code,description) SELECT :id,:insert_code,:description WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE code=:lookup_code)"), {"id":uuid.uuid4(),"insert_code":code,"lookup_code":code,"description":description})
    conn.execute(sa.text("""INSERT INTO role_permissions (role_id,permission_id) SELECT r.id,p.id FROM roles r CROSS JOIN permissions p WHERE r.code='administrator' AND p.code IN ('media.view','media.export','media.manage') AND NOT EXISTS (SELECT 1 FROM role_permissions rp WHERE rp.role_id=r.id AND rp.permission_id=p.id)"""))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN ('media.view','media.export','media.manage'))"))
    op.execute(sa.text("DELETE FROM permissions WHERE code IN ('media.view','media.export','media.manage')"))
    for table in ("media_exports","media_evidence_holds","event_media_references","recording_segments","media_sources","media_storages"):
        op.drop_table(table)
