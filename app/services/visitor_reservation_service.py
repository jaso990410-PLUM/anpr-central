from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from app.models.access_list_entry import AccessListEntry
from app.models.parking import AccessProfile
from app.models.residential import ResidentialSiteSettings, ResidentialUnit, ResidentUnitMembership, VisitorReservation
from app.models.vehicle import Vehicle
from app.services.access_list_service import AccessListService, normalize_plate


class VisitorValidationError(ValueError):
    pass


class VisitorAuthorizationConflict(VisitorValidationError):
    pass


class VisitorReservationService:
    def __init__(self, db: Session):
        self.db = db

    def get(self, reservation_id: UUID, *, lock: bool = False) -> VisitorReservation | None:
        query = select(VisitorReservation).options(
            selectinload(VisitorReservation.site), selectinload(VisitorReservation.unit),
            selectinload(VisitorReservation.host_resident), selectinload(VisitorReservation.access_profile),
            selectinload(VisitorReservation.access_list_entry).selectinload(AccessListEntry.vehicle),
        ).where(VisitorReservation.id == reservation_id)
        if lock:
            query = query.with_for_update()
        return self.db.scalar(query)

    def list(self, *, site_id: UUID | None = None, status: str | None = None) -> tuple[VisitorReservation, ...]:
        query = select(VisitorReservation).options(
            selectinload(VisitorReservation.site), selectinload(VisitorReservation.unit),
            selectinload(VisitorReservation.host_resident)).order_by(VisitorReservation.valid_from.desc())
        if site_id: query = query.where(VisitorReservation.site_id == site_id)
        if status: query = query.where(VisitorReservation.approval_status == status)
        return tuple(self.db.scalars(query).all())

    def create(self, *, site_id: UUID, unit_id: UUID, visitor_name: object, visitor_phone: object,
               purpose: object, plate: object, access_profile_id: UUID | None,
               max_stay_minutes_override: int | None, valid_from: datetime, valid_until: datetime,
               expected_arrival_at: datetime | None, visit_mode: str | None,
               host_resident_id: UUID | None, requested_by_user_id: UUID) -> VisitorReservation:
        unit = self.db.get(ResidentialUnit, unit_id)
        if unit is None or unit.site_id != site_id or unit.status != "ACTIVE":
            raise VisitorValidationError("Select an active unit at this site")
        self._validate_host(unit_id, host_resident_id)
        profile_id, mode = self._defaults(site_id, access_profile_id, visit_mode)
        start, end = _utc(valid_from), _utc(valid_until)
        if end <= start:
            raise VisitorValidationError("Valid until must be later than valid from")
        if expected_arrival_at and not start <= _utc(expected_arrival_at) <= end:
            raise VisitorValidationError("Expected arrival must be within the visit window")
        name = _required(visitor_name, "Visitor name", 150)
        normalized = normalize_plate(str(plate)) if str(plate or "").strip() else None
        item = VisitorReservation(
            site_id=site_id, unit_id=unit_id, host_resident_id=host_resident_id,
            visitor_name=name, visitor_phone=_text(visitor_phone, 40), purpose=_text(purpose, 250),
            plate=normalized, plate_normalized=normalized, access_profile_id=profile_id,
            max_stay_minutes_override=_positive(max_stay_minutes_override), valid_from=start, valid_until=end,
            expected_arrival_at=_utc(expected_arrival_at) if expected_arrival_at else None,
            visit_mode=mode, requested_by_user_id=requested_by_user_id,
        )
        self.db.add(item); self.db.flush(); return item

    def approve(self, reservation_id: UUID, *, actor_user_id: UUID) -> tuple[VisitorReservation, UUID | None]:
        item = self.get(reservation_id, lock=True)
        if item is None: raise VisitorValidationError("Visitor reservation not found")
        if item.approval_status == "APPROVED":
            return item, None
        if item.approval_status != "PENDING":
            raise VisitorValidationError("Only pending reservations can be approved")
        changed = self._bind_access(item) if item.plate_normalized else None
        item.approval_status = "APPROVED"; item.approved_by_user_id = actor_user_id
        item.approved_at = datetime.now(timezone.utc)
        self.db.flush(); return item, changed

    def update_pending(self, reservation_id: UUID, *, visitor_name: object, visitor_phone: object,
                       purpose: object, valid_from: datetime, valid_until: datetime,
                       expected_arrival_at: datetime | None, visit_mode: str,
                       access_profile_id: UUID, max_stay_minutes_override: int | None,
                       host_resident_id: UUID | None) -> VisitorReservation:
        item = self.get(reservation_id, lock=True)
        if item is None: raise VisitorValidationError("Visitor reservation not found")
        if item.approval_status != "PENDING":
            raise VisitorValidationError("Only pending reservations can be edited")
        self._validate_host(item.unit_id, host_resident_id)
        profile_id, mode = self._defaults(item.site_id, access_profile_id, visit_mode)
        start, end = _utc(valid_from), _utc(valid_until)
        if end <= start: raise VisitorValidationError("Valid until must be later than valid from")
        if expected_arrival_at and not start <= _utc(expected_arrival_at) <= end:
            raise VisitorValidationError("Expected arrival must be within the visit window")
        item.visitor_name = _required(visitor_name, "Visitor name", 150)
        item.visitor_phone = _text(visitor_phone, 40); item.purpose = _text(purpose, 250)
        item.host_resident_id = host_resident_id; item.access_profile_id = profile_id
        item.max_stay_minutes_override = _positive(max_stay_minutes_override)
        item.valid_from = start; item.valid_until = end
        item.expected_arrival_at = _utc(expected_arrival_at) if expected_arrival_at else None
        item.visit_mode = mode; self.db.flush(); return item

    def assign_plate(self, reservation_id: UUID, *, plate: str) -> tuple[VisitorReservation, UUID | None]:
        item = self.get(reservation_id, lock=True)
        if item is None: raise VisitorValidationError("Visitor reservation not found")
        if item.approval_status not in {"PENDING", "APPROVED"}:
            raise VisitorValidationError("Plate cannot be assigned to this reservation")
        normalized = normalize_plate(plate)
        if item.plate_normalized and item.plate_normalized != normalized:
            raise VisitorValidationError("Existing plate must be cleared through an explicit edit workflow")
        item.plate = normalized; item.plate_normalized = normalized
        changed = self._bind_access(item) if item.approval_status == "APPROVED" else None
        self.db.flush(); return item, changed

    def reject(self, reservation_id: UUID, *, actor_user_id: UUID, reason: object) -> VisitorReservation:
        item = self.get(reservation_id, lock=True)
        if item is None: raise VisitorValidationError("Visitor reservation not found")
        if item.approval_status == "REJECTED": return item
        if item.approval_status != "PENDING": raise VisitorValidationError("Only pending reservations can be rejected")
        item.approval_status = "REJECTED"; item.rejection_reason = _required(reason, "Rejection reason", 500)
        item.rejected_by_user_id = actor_user_id; item.rejected_at = datetime.now(timezone.utc)
        self.db.flush(); return item

    def cancel(self, reservation_id: UUID, *, actor_user_id: UUID, reason: object) -> tuple[VisitorReservation, UUID | None]:
        item = self.get(reservation_id, lock=True)
        if item is None: raise VisitorValidationError("Visitor reservation not found")
        if item.approval_status == "CANCELLED": return item, None
        if item.approval_status not in {"PENDING", "APPROVED"}:
            raise VisitorValidationError("This reservation cannot be cancelled")
        cancellation = _required(reason, "Cancellation reason", 500)
        changed = None
        if item.access_binding_mode == "TEMPORARY_ACCESS" and item.access_list_entry_id:
            entry = self.db.get(AccessListEntry, item.access_list_entry_id)
            if entry is not None and AccessListService(self.db).set_enabled(entry, False): changed = entry.id
        item.approval_status = "CANCELLED"; item.cancellation_reason = cancellation
        item.cancelled_by_user_id = actor_user_id; item.cancelled_at = datetime.now(timezone.utc)
        self.db.flush(); return item, changed

    def candidates_without_plate(self, *, site_id: UUID, at: datetime) -> tuple[VisitorReservation, ...]:
        return tuple(self.db.scalars(select(VisitorReservation).options(
            selectinload(VisitorReservation.unit), selectinload(VisitorReservation.host_resident)
        ).where(VisitorReservation.site_id == site_id, VisitorReservation.approval_status == "APPROVED",
                VisitorReservation.plate_normalized.is_(None), VisitorReservation.valid_from <= _utc(at),
                VisitorReservation.valid_until >= _utc(at)).order_by(VisitorReservation.expected_arrival_at)).all())

    def _bind_access(self, item: VisitorReservation) -> UUID | None:
        assert item.plate_normalized
        if self.db.get_bind().dialect.name == "postgresql":
            self.db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                            {"key": f"visitor:{item.site_id}:{item.plate_normalized}"})
        existing = AccessListService(self.db).find_for_plate_site(item.plate_normalized, item.site_id)
        if existing is not None:
            # Lock the shared Central record so concurrent approvals resolve deterministically.
            self.db.execute(select(AccessListEntry.id).where(AccessListEntry.id == existing.id).with_for_update())
            if existing.list_type == "BLOCK" and existing.enabled:
                raise VisitorAuthorizationConflict("Approval conflict: this vehicle has an active blacklist record")
            if existing.list_type == "ALLOW" and existing.enabled and _covers(existing, item.valid_from, item.valid_until):
                item.access_list_entry_id = existing.id; item.access_binding_mode = "EXISTING_ACCESS"
                return None
            managed = tuple(self.db.scalars(select(VisitorReservation).where(
                VisitorReservation.id != item.id,
                VisitorReservation.access_list_entry_id == existing.id,
                VisitorReservation.access_binding_mode == "TEMPORARY_ACCESS",
            )).all())
            if managed and existing.list_type == "ALLOW":
                if any(owner.approval_status == "APPROVED" and owner.valid_from < item.valid_until and owner.valid_until > item.valid_from for owner in managed):
                    raise VisitorAuthorizationConflict("Visitor authorization conflict: this plate has an overlapping temporary reservation")
                AccessListService(self.db).update(existing, reference_name=item.visitor_name,
                    list_type="ALLOW", enabled=True, valid_from=item.valid_from, valid_until=item.valid_until,
                    daily_window_enabled=False, camera_comment="VISITOR", access_profile_id=item.access_profile_id,
                    max_stay_minutes_override=item.max_stay_minutes_override)
                item.access_list_entry_id = existing.id; item.access_binding_mode = "TEMPORARY_ACCESS"
                return existing.id
            raise VisitorAuthorizationConflict("Existing Central access record is disabled or does not cover this visit; resolve it in Access List")
        overlap = self.db.scalar(select(VisitorReservation.id).where(
            VisitorReservation.id != item.id, VisitorReservation.site_id == item.site_id,
            VisitorReservation.plate_normalized == item.plate_normalized,
            VisitorReservation.approval_status == "APPROVED",
            VisitorReservation.access_binding_mode == "TEMPORARY_ACCESS",
            VisitorReservation.valid_from < item.valid_until, VisitorReservation.valid_until > item.valid_from))
        if overlap:
            raise VisitorAuthorizationConflict("Visitor authorization conflict: this plate has an overlapping temporary reservation")
        entry = AccessListService(self.db).create(
            plate=item.plate_normalized, site_id=item.site_id, reference_name=item.visitor_name,
            list_type="ALLOW", enabled=True, valid_from=item.valid_from, valid_until=item.valid_until,
            daily_window_enabled=False, camera_comment="VISITOR", access_profile_id=item.access_profile_id,
            max_stay_minutes_override=item.max_stay_minutes_override,
        )
        item.access_list_entry_id = entry.id; item.access_binding_mode = "TEMPORARY_ACCESS"
        return entry.id

    def _validate_host(self, unit_id: UUID, resident_id: UUID | None) -> None:
        if resident_id is None: return
        if self.db.scalar(select(ResidentUnitMembership.id).where(
            ResidentUnitMembership.unit_id == unit_id, ResidentUnitMembership.resident_id == resident_id,
            ResidentUnitMembership.active.is_(True))) is None:
            raise VisitorValidationError("Host must have an active membership for the selected unit")

    def _defaults(self, site_id: UUID, profile_id: UUID | None, visit_mode: str | None) -> tuple[UUID, str]:
        settings = self.db.get(ResidentialSiteSettings, site_id)
        selected_profile = profile_id or (settings.default_visitor_access_profile_id if settings else None)
        mode = visit_mode or (settings.default_visitor_visit_mode if settings else None)
        if selected_profile is None:
            raise VisitorValidationError("Select a visitor access profile or configure a site default")
        profile = self.db.get(AccessProfile, selected_profile)
        if profile is None or profile.site_id != site_id or not profile.enabled:
            raise VisitorValidationError("Select an enabled access profile for this site")
        if mode not in {"SINGLE_VISIT", "MULTI_ENTRY"}:
            raise VisitorValidationError("Select Single Visit or Multi Entry")
        return selected_profile, mode


def _covers(entry: AccessListEntry, start: datetime, end: datetime) -> bool:
    return not entry.daily_window_enabled and (entry.valid_from is None or _utc(entry.valid_from) <= _utc(start)) and (entry.valid_until is None or _utc(entry.valid_until) >= _utc(end))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _text(value: object, limit: int) -> str | None:
    clean = str(value or "").strip()
    if len(clean) > limit: raise VisitorValidationError(f"Value must be at most {limit} characters")
    return clean or None


def _required(value: object, label: str, limit: int) -> str:
    clean = _text(value, limit)
    if not clean: raise VisitorValidationError(f"{label} is required")
    return clean


def _positive(value: int | None) -> int | None:
    if value is None: return None
    if value <= 0: raise VisitorValidationError("Maximum stay must be greater than zero")
    return value
