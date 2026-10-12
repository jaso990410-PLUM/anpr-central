from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ResidentialUnit(Base):
    __tablename__ = "residential_units"
    __table_args__ = (
        UniqueConstraint("site_id", "unit_code_normalized", name="uq_residential_units_site_code"),
        CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="ck_residential_units_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    unit_code: Mapped[str] = mapped_column(String(60), nullable=False)
    unit_code_normalized: Mapped[str] = mapped_column(String(60), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(150))
    block: Mapped[str | None] = mapped_column(String(80))
    floor: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", server_default=text("'ACTIVE'"), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    site = relationship("Site")
    memberships = relationship("ResidentUnitMembership", back_populates="unit")
    vehicle_assignments = relationship("ResidentVehicleAssignment", back_populates="unit")


class Resident(Base):
    __tablename__ = "residents"
    __table_args__ = (CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="ck_residents_status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str] = mapped_column(String(150), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(254))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", server_default=text("'ACTIVE'"), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    memberships = relationship("ResidentUnitMembership", back_populates="resident")
    vehicle_assignments = relationship("ResidentVehicleAssignment", back_populates="resident")


class ResidentUnitMembership(Base):
    __tablename__ = "resident_unit_memberships"
    __table_args__ = (
        CheckConstraint("membership_type IN ('OWNER','TENANT','FAMILY','OCCUPANT','OTHER')", name="ck_resident_memberships_type"),
        Index("ix_resident_memberships_unit_active", "unit_id", "active"),
        Index("uq_resident_memberships_active_pair", "resident_id", "unit_id", unique=True, postgresql_where=text("active = true"), sqlite_where=text("active = 1")),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resident_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("residents.id", ondelete="CASCADE"), index=True)
    unit_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("residential_units.id", ondelete="CASCADE"), index=True)
    membership_type: Mapped[str] = mapped_column(String(16), nullable=False)
    is_primary_contact: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    resident = relationship("Resident", back_populates="memberships")
    unit = relationship("ResidentialUnit", back_populates="memberships")


class ResidentVehicleAssignment(Base):
    __tablename__ = "resident_vehicle_assignments"
    __table_args__ = (
        Index("uq_resident_vehicle_active_entry", "access_list_entry_id", unique=True, postgresql_where=text("active = true"), sqlite_where=text("active = 1")),
        Index("ix_resident_vehicle_unit_active", "unit_id", "active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    access_list_entry_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("access_list_entries.id", ondelete="RESTRICT"), index=True)
    unit_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("residential_units.id", ondelete="CASCADE"), index=True)
    resident_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("residents.id", ondelete="SET NULL"), index=True)
    label: Mapped[str | None] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    access_list_entry = relationship("AccessListEntry")
    unit = relationship("ResidentialUnit", back_populates="vehicle_assignments")
    resident = relationship("Resident", back_populates="vehicle_assignments")


class ResidentialSiteSettings(Base):
    __tablename__ = "residential_site_settings"
    __table_args__ = (CheckConstraint("default_visitor_visit_mode IN ('SINGLE_VISIT','MULTI_ENTRY')", name="ck_residential_site_settings_visit_mode"),)

    site_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True)
    default_resident_access_profile_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("access_profiles.id", ondelete="SET NULL"))
    default_visitor_access_profile_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("access_profiles.id", ondelete="SET NULL"))
    default_visitor_visit_mode: Mapped[str] = mapped_column(String(20), default="SINGLE_VISIT", server_default=text("'SINGLE_VISIT'"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    site = relationship("Site")
    default_resident_access_profile = relationship("AccessProfile", foreign_keys=[default_resident_access_profile_id])
    default_visitor_access_profile = relationship("AccessProfile", foreign_keys=[default_visitor_access_profile_id])


class VisitorReservation(Base):
    __tablename__ = "visitor_reservations"
    __table_args__ = (
        CheckConstraint("valid_until > valid_from", name="ck_visitor_reservations_window"),
        CheckConstraint("visit_mode IN ('SINGLE_VISIT','MULTI_ENTRY')", name="ck_visitor_reservations_mode"),
        CheckConstraint("approval_status IN ('PENDING','APPROVED','REJECTED','CANCELLED')", name="ck_visitor_reservations_approval"),
        CheckConstraint("visit_state IN ('EXPECTED','ARRIVED','DEPARTED','COMPLETED','EXPIRED')", name="ck_visitor_reservations_state"),
        CheckConstraint("access_binding_mode IN ('NONE','TEMPORARY_ACCESS','EXISTING_ACCESS')", name="ck_visitor_reservations_binding"),
        Index("ix_visitor_reservations_site_window", "site_id", "valid_from", "valid_until"),
        Index("ix_visitor_reservations_plate_status", "site_id", "plate_normalized", "approval_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    unit_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("residential_units.id", ondelete="RESTRICT"), index=True)
    host_resident_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("residents.id", ondelete="SET NULL"), index=True)
    visitor_name: Mapped[str] = mapped_column(String(150), nullable=False)
    visitor_phone: Mapped[str | None] = mapped_column(String(40))
    purpose: Mapped[str | None] = mapped_column(String(250))
    plate: Mapped[str | None] = mapped_column(String(32))
    plate_normalized: Mapped[str | None] = mapped_column(String(32))
    access_profile_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("access_profiles.id", ondelete="SET NULL"))
    max_stay_minutes_override: Mapped[int | None] = mapped_column(Integer)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_arrival_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    visit_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    approval_status: Mapped[str] = mapped_column(String(16), default="PENDING", server_default=text("'PENDING'"), nullable=False)
    visit_state: Mapped[str] = mapped_column(String(16), default="EXPECTED", server_default=text("'EXPECTED'"), nullable=False)
    access_list_entry_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("access_list_entries.id", ondelete="SET NULL"), index=True)
    access_binding_mode: Mapped[str] = mapped_column(String(24), default="NONE", server_default=text("'NONE'"), nullable=False)
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(String(500))
    cancelled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(String(500))
    first_arrived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_departed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    site = relationship("Site")
    unit = relationship("ResidentialUnit")
    host_resident = relationship("Resident")
    access_profile = relationship("AccessProfile")
    access_list_entry = relationship("AccessListEntry")
    requested_by = relationship("User", foreign_keys=[requested_by_user_id])
