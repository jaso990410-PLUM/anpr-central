from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, JSON, String, Text, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class RecognitionPlateCorrection(Base):
    __tablename__ = "recognition_plate_corrections"
    __table_args__ = (
        Index("ix_plate_corrections_event_created", "recognition_event_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recognition_event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("recognition_events.id", ondelete="CASCADE"), nullable=False)
    corrected_plate: Mapped[str] = mapped_column(String(32), nullable=False)
    corrected_plate_normalized: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    supersedes_correction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("recognition_plate_corrections.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    recognition_event = relationship("RecognitionEvent")
    created_by = relationship("User", foreign_keys=[created_by_user_id])
    supersedes = relationship("RecognitionPlateCorrection", remote_side=[id])


class ParkingReconciliation(Base):
    __tablename__ = "parking_reconciliations"
    __table_args__ = (
        CheckConstraint(
            "action_type IN ('LINK_EXIT_TO_OPEN_SESSION','CREATE_OPEN_SESSION_FROM_ENTRY','CREATE_CLOSED_SESSION_FROM_ENTRY_EXIT_PAIR','MARK_DUPLICATE_ENTRY','ATTACH_RECOVERED_EVENT_TO_SESSION','RESOLVE_WITHOUT_MATCH')",
            name="ck_parking_reconciliations_action",
        ),
        Index("ix_parking_reconciliations_session", "parking_session_id"),
        Index("ix_parking_reconciliations_primary_event", "primary_recognition_event_id"),
        Index("ix_parking_reconciliations_secondary_event", "secondary_recognition_event_id"),
        Index("ix_parking_reconciliations_performed", "performed_at"),
        UniqueConstraint("action_token", name="uq_parking_reconciliations_action_token"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    action_token: Mapped[str] = mapped_column(String(64), nullable=False)
    primary_recognition_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("recognition_events.id", ondelete="SET NULL"))
    secondary_recognition_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("recognition_events.id", ondelete="SET NULL"))
    parking_session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("parking_sessions.id", ondelete="SET NULL"))
    parking_review_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("parking_review_items.id", ondelete="SET NULL"))
    previous_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    resulting_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    performed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    performed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    site = relationship("Site")
    parking_session = relationship("ParkingSession")
    performed_by = relationship("User")


class VehicleWatch(Base):
    __tablename__ = "vehicle_watches"
    __table_args__ = (
        CheckConstraint("severity IN ('WATCH','HIGH_ATTENTION')", name="ck_vehicle_watches_severity"),
        Index("ix_vehicle_watches_site_plate", "site_id", "plate_normalized"),
        Index("ix_vehicle_watches_active", "active"),
        Index(
            "uq_vehicle_watches_active_site_plate",
            "site_id", "plate_normalized",
            unique=True,
            postgresql_where=text("active = true"),
            sqlite_where=text("active = 1"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    plate_normalized: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(24), nullable=False, default="WATCH", server_default=text("'WATCH'"))
    note: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    cleared_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    site = relationship("Site")
    created_by = relationship("User", foreign_keys=[created_by_user_id])
    cleared_by = relationship("User", foreign_keys=[cleared_by_user_id])
