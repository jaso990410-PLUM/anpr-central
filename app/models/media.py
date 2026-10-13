from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, BigInteger, String, Text, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class MediaStorage(Base):
    __tablename__ = "media_storages"
    __table_args__ = (CheckConstraint("storage_type IN ('LOCAL','NETWORK')", name="ck_media_storages_type"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    storage_type: Mapped[str] = mapped_column(String(16), default="LOCAL")
    root_path: Mapped[str] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    retention_days: Mapped[int | None] = mapped_column(Integer)
    minimum_free_space_gb: Mapped[int] = mapped_column(Integer, default=10, server_default=text("10"))
    minimum_free_space_percent: Mapped[int] = mapped_column(Integer, default=5, server_default=text("5"))
    last_successful_write_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class MediaSource(Base):
    __tablename__ = "media_sources"
    __table_args__ = (
        CheckConstraint("provider_type IN ('NONE','CENTRAL_RECORDER','EXTERNAL_NVR')", name="ck_media_sources_provider"),
        CheckConstraint("recorder_state IN ('DISABLED','STARTING','RECORDING','DEGRADED','DISCONNECTED','ERROR')", name="ck_media_sources_state"),
        UniqueConstraint("camera_id", name="uq_media_sources_camera_id"),
        Index("ix_media_sources_camera_id", "camera_id", unique=True),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    camera_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"))
    provider_type: Mapped[str] = mapped_column(String(24), default="NONE", server_default=text("'NONE'"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    recording_stream_uri: Mapped[str | None] = mapped_column(String(1000))
    live_stream_uri: Mapped[str | None] = mapped_column(String(1000))
    credential_reference: Mapped[str | None] = mapped_column(String(120))
    storage_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("media_storages.id", ondelete="SET NULL"))
    recording_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    pre_event_seconds: Mapped[int] = mapped_column(Integer, default=10, server_default=text("10"))
    post_event_seconds: Mapped[int] = mapped_column(Integer, default=15, server_default=text("15"))
    segment_duration_seconds: Mapped[int] = mapped_column(Integer, default=60, server_default=text("60"))
    media_time_offset_ms: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    expected_codec: Mapped[str | None] = mapped_column(String(30))
    detected_codec: Mapped[str | None] = mapped_column(String(30))
    detected_width: Mapped[int | None] = mapped_column(Integer)
    detected_height: Mapped[int | None] = mapped_column(Integer)
    approximate_bitrate_kbps: Mapped[int | None] = mapped_column(Integer)
    recorder_state: Mapped[str] = mapped_column(String(20), default="DISABLED", server_default=text("'DISABLED'"))
    last_stream_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_segment_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    camera = relationship("Camera")
    storage = relationship("MediaStorage")


class RecordingSegment(Base):
    __tablename__ = "recording_segments"
    __table_args__ = (
        CheckConstraint("state IN ('WRITING','COMPLETE','PARTIAL','FAILED','DELETING')", name="ck_recording_segments_state"),
        Index("ix_recording_segments_camera_started", "camera_id", "started_at"),
        Index("ix_recording_segments_camera_ended", "camera_id", "ended_at"),
        Index("ix_recording_segments_state_started", "state", "started_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    media_source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("media_sources.id", ondelete="CASCADE"), index=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    relative_path: Mapped[str] = mapped_column(String(700), unique=True)
    state: Mapped[str] = mapped_column(String(16), default="WRITING")
    codec: Mapped[str | None] = mapped_column(String(30))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source = relationship("MediaSource")


class EventMediaReference(Base):
    __tablename__ = "event_media_references"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','AVAILABLE','PARTIAL','UNAVAILABLE','EXPIRED','RECORDER_OFFLINE','NOT_CONFIGURED')", name="ck_event_media_references_status"),
        UniqueConstraint("recognition_event_id", name="uq_event_media_references_recognition_event_id"),
        Index("ix_event_media_references_recognition_event_id", "recognition_event_id", unique=True),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recognition_event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("recognition_events.id", ondelete="CASCADE"))
    camera_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    media_source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("media_sources.id", ondelete="SET NULL"))
    media_anchor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    clip_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    clip_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24))
    provider_reference: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    event = relationship("RecognitionEvent")
    source = relationship("MediaSource")


class MediaEvidenceHold(Base):
    __tablename__ = "media_evidence_holds"
    __table_args__ = (Index("ix_media_evidence_holds_camera_window", "camera_id", "start_at", "end_at"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    camera_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    recognition_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("recognition_events.id", ondelete="SET NULL"))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(500))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaExport(Base):
    __tablename__ = "media_exports"
    __table_args__ = (CheckConstraint("state IN ('QUEUED','PROCESSING','COMPLETED','FAILED','EXPIRED')", name="ck_media_exports_state"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    recognition_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("recognition_events.id", ondelete="SET NULL"))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), default="QUEUED", server_default=text("'QUEUED'"))
    output_relative_path: Mapped[str | None] = mapped_column(String(700))
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
