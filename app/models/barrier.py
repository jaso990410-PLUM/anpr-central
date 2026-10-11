from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class BarrierConfiguration(Base):
    __tablename__ = "barrier_configurations"
    __table_args__ = (
        UniqueConstraint("camera_id", name="uq_barrier_configurations_camera_id"),
        CheckConstraint("control_mode IN ('DISABLED','CAMERA_LOCAL','CENTRAL_MANAGED')", name="ck_barrier_configurations_control_mode"),
        CheckConstraint("adapter_type = 'VISIONZENITH_DIRECT_OPEN'", name="ck_barrier_configurations_adapter_type"),
        CheckConstraint("feedback_mode IN ('NONE','NATIVE_GATE_STATUS','GPIO_INPUTS')", name="ck_barrier_configurations_feedback_mode"),
        CheckConstraint("open_active_level IN ('HIGH','LOW')", name="ck_barrier_configurations_open_active_level"),
        CheckConstraint("closed_active_level IN ('HIGH','LOW')", name="ck_barrier_configurations_closed_active_level"),
        CheckConstraint("open_confirmation_timeout_seconds BETWEEN 1 AND 60", name="ck_barrier_configurations_confirmation_timeout"),
        CheckConstraint("open_status_input IS NULL OR open_status_input BETWEEN 0 AND 255", name="ck_barrier_configurations_open_input"),
        CheckConstraint("closed_status_input IS NULL OR closed_status_input BETWEEN 0 AND 255", name="ck_barrier_configurations_closed_input"),
        CheckConstraint("open_status_input IS NULL OR closed_status_input IS NULL OR open_status_input <> closed_status_input", name="ck_barrier_configurations_distinct_inputs"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    camera_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    control_mode: Mapped[str] = mapped_column(String(24), default="DISABLED", server_default=text("'DISABLED'"), nullable=False)
    adapter_type: Mapped[str] = mapped_column(String(40), default="VISIONZENITH_DIRECT_OPEN", server_default=text("'VISIONZENITH_DIRECT_OPEN'"), nullable=False)
    feedback_mode: Mapped[str] = mapped_column(String(24), default="NONE", server_default=text("'NONE'"), nullable=False)
    commissioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    commissioned_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    local_auto_open_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    local_auto_open_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_auto_open_verified_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    commissioning_notes: Mapped[str | None] = mapped_column(Text)
    open_confirmation_timeout_seconds: Mapped[int] = mapped_column(Integer, default=10, server_default=text("10"), nullable=False)
    open_status_input: Mapped[int | None] = mapped_column(Integer)
    open_active_level: Mapped[str] = mapped_column(String(4), default="HIGH", server_default=text("'HIGH'"), nullable=False)
    closed_status_input: Mapped[int | None] = mapped_column(Integer)
    closed_active_level: Mapped[str] = mapped_column(String(4), default="HIGH", server_default=text("'HIGH'"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    camera = relationship("Camera")


class BarrierAction(Base):
    __tablename__ = "barrier_actions"
    __table_args__ = (
        CheckConstraint("action_type = 'OPEN'", name="ck_barrier_actions_action_type"),
        CheckConstraint("trigger_source IN ('AUTO_DECISION','MANUAL_RELEASE','COMMISSIONING_TEST')", name="ck_barrier_actions_trigger_source"),
        CheckConstraint("command_status IN ('QUEUED','PUBLISHED','CAMERA_ACKNOWLEDGED','CAMERA_REJECTED','PUBLISH_FAILED','TIMEOUT_UNCERTAIN','EXPIRED_NOT_SENT','SUPPRESSED_ALREADY_OPEN','NOT_CONFIGURED')", name="ck_barrier_actions_command_status"),
        CheckConstraint("physical_confirmation_status IN ('NOT_AVAILABLE','PENDING','CONFIRMED_OPEN','FEEDBACK_NOT_OBSERVED')", name="ck_barrier_actions_physical_status"),
        Index("ix_barrier_actions_camera_requested", "camera_id", "requested_at"),
        Index("ix_barrier_actions_command_status", "command_status"),
        Index("ix_barrier_actions_trigger_requested", "trigger_source", "requested_at"),
        Index("uq_barrier_actions_auto_decision", "access_decision_id", unique=True, postgresql_where=text("trigger_source = 'AUTO_DECISION'"), sqlite_where=text("trigger_source = 'AUTO_DECISION'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    camera_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    recognition_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("recognition_events.id", ondelete="SET NULL"))
    access_decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("access_decisions.id", ondelete="SET NULL"), index=True)
    parking_review_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("parking_review_items.id", ondelete="SET NULL"))
    action_type: Mapped[str] = mapped_column(String(16), default="OPEN", server_default=text("'OPEN'"), nullable=False)
    trigger_source: Mapped[str] = mapped_column(String(24), nullable=False)
    command_status: Mapped[str] = mapped_column(String(32), default="QUEUED", server_default=text("'QUEUED'"), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(80), unique=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    camera_ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    physical_confirmation_status: Mapped[str] = mapped_column(String(32), default="NOT_AVAILABLE", server_default=text("'NOT_AVAILABLE'"), nullable=False)
    physical_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    operator_reason: Mapped[str | None] = mapped_column(String(500))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    camera = relationship("Camera")
    site = relationship("Site")
    recognition_event = relationship("RecognitionEvent")
    access_decision = relationship("AccessDecision")
    requested_by = relationship("User")


class BarrierState(Base):
    __tablename__ = "barrier_states"
    __table_args__ = (
        UniqueConstraint("camera_id", name="uq_barrier_states_camera_id"),
        CheckConstraint("normalized_state IN ('UNKNOWN','CLOSED','OPEN','INTERMEDIATE','INVALID_FEEDBACK')", name="ck_barrier_states_normalized_state"),
        CheckConstraint("feedback_source IN ('NONE','NATIVE_GATE_STATUS','GPIO_INPUTS')", name="ck_barrier_states_feedback_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    camera_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    normalized_state: Mapped[str] = mapped_column(String(24), default="UNKNOWN", server_default=text("'UNKNOWN'"), nullable=False)
    feedback_source: Mapped[str] = mapped_column(String(24), default="NONE", server_default=text("'NONE'"), nullable=False)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_known_state: Mapped[str | None] = mapped_column(String(24))
    last_known_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connect_status: Mapped[int | None] = mapped_column(Integer)
    gate_enabled: Mapped[bool | None] = mapped_column(Boolean)
    gate_ctrl_id: Mapped[str | None] = mapped_column(String(100))
    open_input_raw: Mapped[int | None] = mapped_column(Integer)
    closed_input_raw: Mapped[int | None] = mapped_column(Integer)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    camera = relationship("Camera")
