from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models.access_list_entry import AccessListEntry
from app.models.parking import AccessProfile
from app.models.residential import (
    Resident,
    ResidentialSiteSettings,
    ResidentialUnit,
    ResidentUnitMembership,
    ResidentVehicleAssignment,
)
from app.models.site import Site
from app.services.access_list_service import AccessListService


MEMBERSHIP_TYPES = {"OWNER", "TENANT", "FAMILY", "OCCUPANT", "OTHER"}
VISIT_MODES = {"SINGLE_VISIT", "MULTI_ENTRY"}


class ResidentialValidationError(ValueError):
    pass


def normalize_unit_code(value: object) -> str:
    code = re.sub(r"\s+", "", str(value or "").strip().upper())
    if not code or len(code) > 60 or not re.fullmatch(r"[A-Z0-9][A-Z0-9._/-]*", code):
        raise ResidentialValidationError("Unit code must contain letters, numbers, dots, slashes or hyphens")
    return code


class ResidentialIdentityService:
    def __init__(self, db: Session):
        self.db = db

    def sites(self) -> tuple[Site, ...]:
        return tuple(self.db.scalars(select(Site).where(Site.is_active.is_(True)).order_by(Site.code)).all())

    def units(self, site_id: UUID | None = None) -> tuple[ResidentialUnit, ...]:
        query = select(ResidentialUnit).options(selectinload(ResidentialUnit.site)).order_by(ResidentialUnit.unit_code_normalized)
        if site_id:
            query = query.where(ResidentialUnit.site_id == site_id)
        return tuple(self.db.scalars(query).all())

    def unit(self, unit_id: UUID) -> ResidentialUnit | None:
        return self.db.scalar(select(ResidentialUnit).options(
            selectinload(ResidentialUnit.site),
            selectinload(ResidentialUnit.memberships).selectinload(ResidentUnitMembership.resident),
            selectinload(ResidentialUnit.vehicle_assignments).selectinload(ResidentVehicleAssignment.access_list_entry).selectinload(AccessListEntry.vehicle),
        ).where(ResidentialUnit.id == unit_id))

    def create_unit(self, *, site_id: UUID, unit_code: object, display_name: object = None,
                    block: object = None, floor: object = None, notes: object = None) -> ResidentialUnit:
        site = self.db.get(Site, site_id)
        if site is None or not site.is_active:
            raise ResidentialValidationError("Select an active site")
        normalized = normalize_unit_code(unit_code)
        if self.db.scalar(select(ResidentialUnit).where(ResidentialUnit.site_id == site_id, ResidentialUnit.unit_code_normalized == normalized)):
            raise ResidentialValidationError("This unit code already exists at the selected site")
        unit = ResidentialUnit(site_id=site_id, unit_code=str(unit_code).strip(), unit_code_normalized=normalized,
                               display_name=_text(display_name, 150), block=_text(block, 80),
                               floor=_text(floor, 40), notes=_text(notes, 2000))
        self.db.add(unit); self.db.flush()
        return unit

    def set_unit_active(self, unit: ResidentialUnit, active: bool, *, disable_access: bool = False) -> tuple[UUID, ...]:
        unit.status = "ACTIVE" if active else "INACTIVE"
        changed: list[UUID] = []
        if not active and disable_access:
            assignments = self.db.scalars(select(ResidentVehicleAssignment).where(
                ResidentVehicleAssignment.unit_id == unit.id, ResidentVehicleAssignment.active.is_(True)
            )).all()
            access = AccessListService(self.db)
            for assignment in assignments:
                entry = self.db.get(AccessListEntry, assignment.access_list_entry_id)
                if entry is not None and access.set_enabled(entry, False):
                    changed.append(entry.id)
        self.db.flush()
        return tuple(changed)

    def update_unit(self, unit: ResidentialUnit, *, unit_code: object, display_name: object = None,
                    block: object = None, floor: object = None, notes: object = None) -> None:
        normalized = normalize_unit_code(unit_code)
        duplicate = self.db.scalar(select(ResidentialUnit.id).where(
            ResidentialUnit.site_id == unit.site_id,
            ResidentialUnit.unit_code_normalized == normalized,
            ResidentialUnit.id != unit.id,
        ))
        if duplicate:
            raise ResidentialValidationError("This unit code already exists at the selected site")
        unit.unit_code = str(unit_code).strip(); unit.unit_code_normalized = normalized
        unit.display_name = _text(display_name, 150); unit.block = _text(block, 80)
        unit.floor = _text(floor, 40); unit.notes = _text(notes, 2000)
        self.db.flush()

    def residents(self) -> tuple[Resident, ...]:
        return tuple(self.db.scalars(select(Resident).order_by(Resident.display_name)).all())

    def resident(self, resident_id: UUID) -> Resident | None:
        return self.db.scalar(select(Resident).options(
            selectinload(Resident.memberships).selectinload(ResidentUnitMembership.unit).selectinload(ResidentialUnit.site),
            selectinload(Resident.vehicle_assignments).selectinload(ResidentVehicleAssignment.access_list_entry).selectinload(AccessListEntry.vehicle),
        ).where(Resident.id == resident_id))

    def create_resident(self, *, display_name: object, phone: object = None, email: object = None,
                        notes: object = None) -> Resident:
        name = _required(display_name, "Resident name", 150)
        resident = Resident(display_name=name, phone=_text(phone, 40), email=_email(email), notes=_text(notes, 2000))
        self.db.add(resident); self.db.flush()
        return resident

    def update_resident(self, resident: Resident, **values: object) -> None:
        resident.display_name = _required(values.get("display_name"), "Resident name", 150)
        resident.phone = _text(values.get("phone"), 40)
        resident.email = _email(values.get("email"))
        resident.notes = _text(values.get("notes"), 2000)
        self.db.flush()

    def set_resident_active(self, resident: Resident, active: bool, *, disable_access: bool = False) -> tuple[UUID, ...]:
        resident.status = "ACTIVE" if active else "INACTIVE"
        changed: list[UUID] = []
        if not active and disable_access:
            assignments = self.db.scalars(select(ResidentVehicleAssignment).where(
                ResidentVehicleAssignment.resident_id == resident.id, ResidentVehicleAssignment.active.is_(True)
            )).all()
            access = AccessListService(self.db)
            for assignment in assignments:
                entry = self.db.get(AccessListEntry, assignment.access_list_entry_id)
                if entry is not None and access.set_enabled(entry, False):
                    changed.append(entry.id)
        self.db.flush()
        return tuple(changed)

    def add_membership(self, *, resident_id: UUID, unit_id: UUID, membership_type: object,
                       primary: bool = False) -> ResidentUnitMembership:
        resident = self.db.get(Resident, resident_id); unit = self.db.get(ResidentialUnit, unit_id)
        kind = str(membership_type or "").strip().upper()
        if resident is None or resident.status != "ACTIVE" or unit is None or unit.status != "ACTIVE":
            raise ResidentialValidationError("Select an active resident and unit")
        if kind not in MEMBERSHIP_TYPES:
            raise ResidentialValidationError("Select a valid membership type")
        existing = self.db.scalar(select(ResidentUnitMembership).where(
            ResidentUnitMembership.resident_id == resident_id, ResidentUnitMembership.unit_id == unit_id,
            ResidentUnitMembership.active.is_(True)))
        if existing:
            raise ResidentialValidationError("This resident already has an active membership for the unit")
        item = ResidentUnitMembership(resident_id=resident_id, unit_id=unit_id,
                                      membership_type=kind, is_primary_contact=primary, active=True)
        self.db.add(item); self.db.flush()
        return item

    def end_membership(self, membership: ResidentUnitMembership) -> bool:
        if not membership.active:
            return False
        membership.active = False
        membership.valid_until = datetime.now(timezone.utc)
        self.db.flush(); return True

    def link_vehicle(self, *, unit_id: UUID, access_entry_id: UUID, resident_id: UUID | None = None,
                     label: object = None) -> ResidentVehicleAssignment:
        unit = self.db.get(ResidentialUnit, unit_id); entry = self.db.get(AccessListEntry, access_entry_id)
        if unit is None or entry is None or entry.site_id != unit.site_id:
            raise ResidentialValidationError("Vehicle access entry must belong to the unit's site")
        if resident_id is not None:
            active_membership = self.db.scalar(select(ResidentUnitMembership.id).where(
                ResidentUnitMembership.resident_id == resident_id, ResidentUnitMembership.unit_id == unit_id,
                ResidentUnitMembership.active.is_(True)))
            if active_membership is None:
                raise ResidentialValidationError("Selected resident is not an active member of this unit")
        existing = self.db.scalar(select(ResidentVehicleAssignment).where(
            ResidentVehicleAssignment.access_list_entry_id == access_entry_id,
            ResidentVehicleAssignment.active.is_(True)))
        if existing:
            raise ResidentialValidationError("This access record is already assigned to a residential unit")
        item = ResidentVehicleAssignment(access_list_entry_id=access_entry_id, unit_id=unit_id,
                                          resident_id=resident_id, label=_text(label, 100), active=True)
        self.db.add(item); self.db.flush(); return item

    def unlink_vehicle(self, assignment: ResidentVehicleAssignment, *, disable_access: bool = False) -> UUID | None:
        if not assignment.active:
            return None
        assignment.active = False
        changed = None
        if disable_access:
            entry = self.db.get(AccessListEntry, assignment.access_list_entry_id)
            if entry is not None and AccessListService(self.db).set_enabled(entry, False):
                changed = entry.id
        self.db.flush(); return changed

    def settings(self, site_id: UUID) -> ResidentialSiteSettings | None:
        return self.db.get(ResidentialSiteSettings, site_id)

    def save_settings(self, *, site_id: UUID, resident_profile_id: UUID | None,
                      visitor_profile_id: UUID | None, visit_mode: str) -> ResidentialSiteSettings:
        if visit_mode not in VISIT_MODES:
            raise ResidentialValidationError("Select a valid default visitor mode")
        for profile_id in (resident_profile_id, visitor_profile_id):
            if profile_id is not None:
                profile = self.db.get(AccessProfile, profile_id)
                if profile is None or profile.site_id != site_id or not profile.enabled:
                    raise ResidentialValidationError("Default profiles must be enabled and belong to this site")
        item = self.db.get(ResidentialSiteSettings, site_id)
        if item is None:
            item = ResidentialSiteSettings(site_id=site_id); self.db.add(item)
        item.default_resident_access_profile_id = resident_profile_id
        item.default_visitor_access_profile_id = visitor_profile_id
        item.default_visitor_visit_mode = visit_mode
        self.db.flush(); return item


def _text(value: object, limit: int) -> str | None:
    clean = str(value or "").strip()
    if len(clean) > limit:
        raise ResidentialValidationError(f"Value must be at most {limit} characters")
    return clean or None


def _required(value: object, label: str, limit: int) -> str:
    clean = _text(value, limit)
    if not clean:
        raise ResidentialValidationError(f"{label} is required")
    return clean


def _email(value: object) -> str | None:
    email = _text(value, 254)
    if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ResidentialValidationError("Enter a valid email address")
    return email
