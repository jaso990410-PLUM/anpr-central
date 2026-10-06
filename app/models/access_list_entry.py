from __future__ import annotations

import uuid
from datetime import datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Time, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.access_list_camera_sync import AccessListCameraSync
    from app.models.site import Site
    from app.models.vehicle import Vehicle


class AccessListEntry(Base):
    __tablename__ = "access_list_entries"
    __table_args__ = (
        UniqueConstraint("vehicle_id", "site_id", name="uq_access_list_entry_vehicle_site"),
        Index("ix_access_list_entries_list_type", "list_type"),
        Index("ix_access_list_entries_enabled", "enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("vehicles.id", ondelete="CASCADE"), index=True, nullable=False)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), index=True, nullable=False)
    list_type: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    daily_window_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    daily_start: Mapped[time | None] = mapped_column(Time())
    daily_end: Mapped[time | None] = mapped_column(Time())
    camera_comment: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    vehicle: Mapped["Vehicle"] = relationship(back_populates="access_entries")
    site: Mapped["Site"] = relationship()
    camera_syncs: Mapped[list["AccessListCameraSync"]] = relationship(back_populates="access_entry", cascade="all, delete-orphan")
