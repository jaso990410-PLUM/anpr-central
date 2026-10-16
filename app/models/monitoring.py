from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class UnifiedEvent(Base):
    """Compact searchable projection; authoritative detail stays in the source model."""

    __tablename__ = "unified_events"
    __table_args__ = (
        CheckConstraint("category IN ('TRAFFIC_ANPR','ACCESS_SECURITY','ADMIN_BUSINESS','OPERATOR_ACTION','SYSTEM_HEALTH')", name="category"),
        CheckConstraint("severity IN ('INFO','WARNING','CRITICAL')", name="severity"),
        UniqueConstraint("source_type", "source_id", "event_type", name="uq_unified_events_source_event"),
        Index("ix_unified_events_occurred_at", "occurred_at"),
        Index("ix_unified_events_site_occurred", "site_id", "occurred_at"),
        Index("ix_unified_events_category_occurred", "category", "occurred_at"),
        Index("ix_unified_events_attention_occurred", "attention_required", "occurred_at"),
        Index("ix_unified_events_source", "source_type", "source_id"),
        Index("ix_unified_events_correlation", "correlation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    attention_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    site_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("sites.id", ondelete="SET NULL"))
    lane_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    camera_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("cameras.id", ondelete="SET NULL"))
    source_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source_id: Mapped[str] = mapped_column(String(160), nullable=False)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    display_subject: Mapped[str] = mapped_column(String(180), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    audit_significant: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    site = relationship("Site")
    camera = relationship("Camera")


class OperationalAlert(Base):
    __tablename__ = "operational_alerts"
    __table_args__ = (
        CheckConstraint("severity IN ('WARNING','CRITICAL')", name="severity"),
        CheckConstraint("status IN ('ACTIVE','ACKNOWLEDGED','RESOLVED')", name="status"),
        UniqueConstraint("fingerprint", name="uq_operational_alerts_fingerprint"),
        Index("ix_operational_alerts_status", "status"),
        Index("ix_operational_alerts_severity", "severity"),
        Index("ix_operational_alerts_site_status", "site_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    alert_type: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    site_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("sites.id", ondelete="SET NULL"))
    component_type: Mapped[str] = mapped_column(String(60), nullable=False)
    component_id: Mapped[str] = mapped_column(String(160), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(320), nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    acknowledgement_note: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_reason: Mapped[str | None] = mapped_column(String(500))
    trigger_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("unified_events.id", ondelete="SET NULL"))
    resolution_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("unified_events.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    site = relationship("Site")
    acknowledged_by = relationship("User")
    trigger_event = relationship("UnifiedEvent", foreign_keys=[trigger_event_id])
    resolution_event = relationship("UnifiedEvent", foreign_keys=[resolution_event_id])


class ComponentHealth(Base):
    __tablename__ = "component_health"
    __table_args__ = (
        CheckConstraint("health_state IN ('HEALTHY','DEGRADED','FAULT','UNKNOWN','UNVERIFIED','DISABLED')", name="state"),
        UniqueConstraint("component_type", "component_id", name="uq_component_health_identity"),
        Index("ix_component_health_site_state", "site_id", "health_state"),
        Index("ix_component_health_observed", "observed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    component_type: Mapped[str] = mapped_column(String(60), nullable=False)
    component_id: Mapped[str] = mapped_column(String(160), nullable=False)
    site_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("sites.id", ondelete="SET NULL"))
    display_name: Mapped[str] = mapped_column(String(180), nullable=False)
    health_state: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    capabilities: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    site = relationship("Site")
