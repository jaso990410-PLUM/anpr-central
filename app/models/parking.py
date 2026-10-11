from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class DecisionResponseProfile(Base):
    __tablename__ = "decision_response_profiles"
    __table_args__ = (
        UniqueConstraint("site_id", "name", name="uq_decision_response_profile_site_name"),
        CheckConstraint("led_brightness BETWEEN 0 AND 5", name="ck_decision_response_profiles_led_brightness"),
        CheckConstraint("led_scroll_speed BETWEEN 0 AND 5", name="ck_decision_response_profiles_led_scroll_speed"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    response_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    led_rows: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    led_brightness: Mapped[int] = mapped_column(Integer, default=5, server_default=text("5"), nullable=False)
    led_scroll_speed: Mapped[int] = mapped_column(Integer, default=5, server_default=text("5"), nullable=False)
    display_seconds: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    traffic_state: Mapped[str] = mapped_column(String(8), default="RED", nullable=False)
    traffic_green_seconds: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    speaker_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    speaker_message: Mapped[str] = mapped_column(String(250), default="{plate}", nullable=False)
    speaker_volume: Mapped[int] = mapped_column(Integer, default=7, nullable=False)
    speaker_speed: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    speaker_voice: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    speaker_pitch: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    plate_pause_ms: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class SitePolicy(Base):
    __tablename__ = "site_policies"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), unique=True)
    operating_mode: Mapped[str] = mapped_column(String(24), default="RESIDENTIAL", nullable=False)
    unknown_entry_policy: Mapped[str] = mapped_column(String(16), default="DENY", nullable=False)
    exit_mode: Mapped[str] = mapped_column(String(16), default="FREE_EXIT", nullable=False)
    exit_blacklist_policy: Mapped[str] = mapped_column(String(16), default="DENY", nullable=False)
    allow_response_profile_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("decision_response_profiles.id", ondelete="SET NULL"))
    deny_response_profile_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("decision_response_profiles.id", ondelete="SET NULL"))
    review_response_profile_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("decision_response_profiles.id", ondelete="SET NULL"))
    policy_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AccessProfile(Base):
    __tablename__ = "access_profiles"
    __table_args__ = (UniqueConstraint("site_id", "name", name="uq_access_profile_site_name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    default_max_stay_minutes: Mapped[int | None] = mapped_column(Integer)
    overstay_monitoring_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    normal_exit_policy: Mapped[str] = mapped_column(String(20), default="USE_SITE_POLICY", nullable=False)
    expired_exit_policy: Mapped[str] = mapped_column(String(20), default="USE_SITE_POLICY", nullable=False)
    overstay_exit_policy: Mapped[str] = mapped_column(String(20), default="REVIEW", nullable=False)
    blacklist_exit_policy: Mapped[str] = mapped_column(String(20), default="DENY", nullable=False)
    overstay_exit_grace_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AccessDecision(Base):
    __tablename__ = "access_decisions"
    __table_args__ = (Index("ix_access_decisions_site_evaluated", "site_id", "evaluated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recognition_event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("recognition_events.id", ondelete="CASCADE"), unique=True)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("vehicles.id", ondelete="SET NULL"), index=True)
    access_entry_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("access_list_entries.id", ondelete="SET NULL"))
    access_status: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_type: Mapped[str] = mapped_column(String(24), nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    response_profile_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("decision_response_profiles.id", ondelete="SET NULL"))
    response_status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)
    exit_policy_source: Mapped[str | None] = mapped_column(String(40))
    barrier_intent: Mapped[str] = mapped_column(String(24), default="NONE", nullable=False)
    physical_execution: Mapped[str] = mapped_column(String(24), default="NOT_IMPLEMENTED", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    recognition_event = relationship("RecognitionEvent")
    response_profile = relationship("DecisionResponseProfile")


class ParkingSession(Base):
    __tablename__ = "parking_sessions"
    __table_args__ = (
        Index("ix_parking_sessions_site_status", "site_id", "status"),
        Index("uq_parking_sessions_active_vehicle_site", "site_id", "vehicle_id", unique=True, postgresql_where=text("status IN ('OPEN', 'OVERSTAY')")),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("vehicles.id", ondelete="CASCADE"), index=True)
    access_profile_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("access_profiles.id", ondelete="SET NULL"))
    entry_recognition_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("recognition_events.id", ondelete="RESTRICT"))
    exit_recognition_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("recognition_events.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False)
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_stay_minutes: Mapped[int | None] = mapped_column(Integer)
    overstay_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    overstay_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_normal_exit_policy: Mapped[str] = mapped_column(String(20), default="USE_SITE_POLICY", nullable=False)
    effective_expired_exit_policy: Mapped[str] = mapped_column(String(20), default="USE_SITE_POLICY", nullable=False)
    effective_overstay_exit_policy: Mapped[str] = mapped_column(String(20), default="REVIEW", nullable=False)
    effective_blacklist_exit_policy: Mapped[str] = mapped_column(String(20), default="DENY", nullable=False)
    effective_overstay_exit_grace_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(24), default="LIVE", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    vehicle = relationship("Vehicle")
    site = relationship("Site")
    access_profile = relationship("AccessProfile")
    entry_recognition = relationship("RecognitionEvent", foreign_keys=[entry_recognition_id])
    exit_recognition = relationship("RecognitionEvent", foreign_keys=[exit_recognition_id])


class ParkingSessionIssue(Base):
    __tablename__ = "parking_session_issues"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("vehicles.id", ondelete="SET NULL"), index=True)
    recognition_event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("recognition_events.id", ondelete="CASCADE"), unique=True)
    issue_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class OverstayAlert(Base):
    __tablename__ = "overstay_alerts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    parking_session_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("parking_sessions.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    parking_session = relationship("ParkingSession")


class ParkingReviewItem(Base):
    __tablename__ = "parking_review_items"
    __table_args__ = (
        Index("ix_parking_review_items_site_status", "site_id", "status"),
        Index("ix_parking_review_items_session_type", "parking_session_id", "item_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("vehicles.id", ondelete="SET NULL"), index=True)
    parking_session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("parking_sessions.id", ondelete="SET NULL"), index=True)
    recognition_event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("recognition_events.id", ondelete="CASCADE"))
    access_decision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("access_decisions.id", ondelete="CASCADE"))
    item_type: Mapped[str] = mapped_column(String(40), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False)
    first_triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    site = relationship("Site")
    vehicle = relationship("Vehicle")
    parking_session = relationship("ParkingSession")
    access_decision = relationship("AccessDecision")
