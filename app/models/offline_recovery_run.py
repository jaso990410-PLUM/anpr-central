from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.camera import Camera


class OfflineRecoveryRun(Base):
    __tablename__ = "offline_recovery_runs"
    __table_args__ = (Index("ix_offline_recovery_camera_started", "camera_id", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    camera_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    initial_reported_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_reported_count: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_max_count: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    requested_min_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    events_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicates_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    batch_start_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    batch_start_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    command_request_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    camera: Mapped["Camera"] = relationship()
