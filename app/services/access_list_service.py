import math
import re
from dataclasses import dataclass
from datetime import datetime, time, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.access_list_camera_sync import AccessListCameraSync
from app.models.access_list_entry import AccessListEntry
from app.models.camera import Camera
from app.models.site import Site
from app.models.vehicle import Vehicle


PAGE_SIZE = 50
LIST_TYPES = {"ALLOW", "BLOCK"}


class AccessListValidationError(ValueError):
    pass


@dataclass(frozen=True)
class AccessListPage:
    items: tuple[AccessListEntry, ...]
    total: int
    page: int
    page_count: int


class AccessListService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def sites(self) -> tuple[Site, ...]:
        return tuple(self.db.scalars(select(Site).where(Site.is_active.is_(True)).order_by(Site.code)).all())

    def get(self, entry_id: UUID) -> AccessListEntry | None:
        return self.db.scalar(
            select(AccessListEntry)
            .options(
                selectinload(AccessListEntry.vehicle),
                selectinload(AccessListEntry.site),
                selectinload(AccessListEntry.camera_syncs).selectinload(AccessListCameraSync.camera),
            )
            .where(AccessListEntry.id == entry_id)
        )

    def find_for_plate_site(
        self, plate: str, site_id: str | UUID
    ) -> AccessListEntry | None:
        try:
            normalized = normalize_plate(plate)
            parsed_site_id = UUID(str(site_id))
        except (AccessListValidationError, ValueError):
            return None
        return self.db.scalar(
            select(AccessListEntry)
            .join(Vehicle)
            .options(
                selectinload(AccessListEntry.vehicle),
                selectinload(AccessListEntry.site),
                selectinload(AccessListEntry.camera_syncs),
            )
            .where(
                Vehicle.plate_text == normalized,
                AccessListEntry.site_id == parsed_site_id,
            )
        )

    def list_entries(
        self,
        *,
        plate: str | None = None,
        site_id: UUID | None = None,
        list_type: str | None = None,
        enabled: bool | None = None,
        sync_status: str | None = None,
        page: int = 1,
    ) -> AccessListPage:
        conditions = []
        clean_plate = (plate or "").strip().upper()
        if clean_plate:
            conditions.append(Vehicle.plate_text.ilike(f"%{clean_plate}%"))
        if site_id:
            conditions.append(AccessListEntry.site_id == site_id)
        if list_type:
            conditions.append(AccessListEntry.list_type == list_type.upper())
        if enabled is not None:
            conditions.append(AccessListEntry.enabled.is_(enabled))
        if sync_status:
            conditions.append(
                AccessListEntry.camera_syncs.any(AccessListCameraSync.status == sync_status.upper())
            )
        count_query = select(func.count()).select_from(AccessListEntry).join(Vehicle).where(*conditions)
        total = self.db.scalar(count_query) or 0
        page_count = max(1, math.ceil(total / PAGE_SIZE))
        page = min(max(page, 1), page_count)
        items = self.db.scalars(
            select(AccessListEntry)
            .join(Vehicle)
            .options(selectinload(AccessListEntry.vehicle), selectinload(AccessListEntry.site), selectinload(AccessListEntry.camera_syncs))
            .where(*conditions)
            .order_by(Vehicle.plate_text)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        ).all()
        return AccessListPage(tuple(items), total, page, page_count)

    def create(
        self,
        *,
        plate: str,
        site_id: str | UUID,
        reference_name: object = None,
        **values: object,
    ) -> AccessListEntry:
        normalized = normalize_plate(plate)
        clean_reference_name = normalize_reference_name(reference_name)
        site = self._site(site_id)
        vehicle = self.db.scalar(select(Vehicle).where(Vehicle.plate_text == normalized))
        if vehicle is None:
            vehicle = Vehicle(plate_text=normalized, reference_name=clean_reference_name)
            self.db.add(vehicle)
            self.db.flush()
        elif clean_reference_name:
            vehicle.reference_name = clean_reference_name
        if self.db.scalar(select(AccessListEntry).where(AccessListEntry.vehicle_id == vehicle.id, AccessListEntry.site_id == site.id)):
            raise AccessListValidationError("This plate already has an access entry for the selected site")
        clean = validate_entry_values(site, **values)
        entry = AccessListEntry(vehicle=vehicle, site=site, **clean)
        self.db.add(entry)
        self.db.flush()
        self.ensure_sync_records(entry)
        return entry

    def update(
        self,
        entry: AccessListEntry,
        *,
        reference_name: object = None,
        **values: object,
    ) -> dict[str, object]:
        clean = validate_entry_values(entry.site, **values)
        clean_reference_name = normalize_reference_name(reference_name)
        before = entry_summary(entry)
        entry.vehicle.reference_name = clean_reference_name
        for key, value in clean.items():
            setattr(entry, key, value)
        self.ensure_sync_records(entry, reset=True)
        self.db.flush()
        return before

    def set_enabled(self, entry: AccessListEntry, enabled: bool) -> bool:
        if entry.enabled == enabled:
            return False
        entry.enabled = enabled
        self.ensure_sync_records(entry, reset=True)
        self.db.flush()
        return True

    def ensure_sync_records(self, entry: AccessListEntry, *, reset: bool = False) -> tuple[AccessListCameraSync, ...]:
        cameras = self.db.scalars(
            select(Camera).where(
                Camera.site_id == entry.site_id,
                Camera.access_list_sync_enabled.is_(True),
            )
        ).all()
        existing = {item.camera_id: item for item in entry.camera_syncs}
        result = []
        for camera in cameras:
            sync = existing.get(camera.id)
            if sync is None:
                sync = AccessListCameraSync(access_entry=entry, camera_id=camera.id, status="PENDING")
                self.db.add(sync)
            elif reset and sync.status != "TIMEOUT_UNCERTAIN":
                sync.status = "PENDING"
                sync.last_error = None
            result.append(sync)
        return tuple(result)

    def _site(self, site_id: str | UUID) -> Site:
        try:
            parsed = UUID(str(site_id))
        except ValueError as error:
            raise AccessListValidationError("Select a valid site") from error
        site = self.db.get(Site, parsed)
        if site is None or not site.is_active:
            raise AccessListValidationError("Select a valid active site")
        return site


def normalize_plate(value: str) -> str:
    plate = (value or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,32}", plate):
        raise AccessListValidationError("Plate must contain 1 to 32 letters or digits")
    return plate


def normalize_reference_name(value: object) -> str | None:
    name = str(value or "").strip()
    if len(name) > 120:
        raise AccessListValidationError("Name must be at most 120 characters")
    return name or None


def validate_entry_values(
    site: Site,
    *,
    list_type: object,
    enabled: object,
    valid_from: object = None,
    valid_until: object = None,
    daily_window_enabled: object = False,
    daily_start: object = None,
    daily_end: object = None,
    camera_comment: object = None,
) -> dict[str, object]:
    normalized_type = str(list_type).strip().upper()
    if normalized_type not in LIST_TYPES:
        raise AccessListValidationError("Access type must be Allow or Block")
    start = _local_datetime(valid_from, site.timezone, "Valid from")
    end = _local_datetime(valid_until, site.timezone, "Valid until")
    if start and end and start >= end:
        raise AccessListValidationError("Valid until must be later than valid from")
    window_enabled = bool(daily_window_enabled)
    window_start = _clock(daily_start, "Daily start")
    window_end = _clock(daily_end, "Daily end")
    if window_enabled and (window_start is None or window_end is None):
        raise AccessListValidationError("Daily start and end are required when the daily window is enabled")
    if window_enabled and window_start == window_end:
        raise AccessListValidationError("Daily start and end must differ")
    if not window_enabled:
        window_start = None
        window_end = None
    comment = str(camera_comment or "").strip()
    if len(comment) > 16:
        raise AccessListValidationError("Camera remark must be at most 16 characters")
    return {
        "list_type": normalized_type,
        "enabled": bool(enabled),
        "valid_from": start,
        "valid_until": end,
        "daily_window_enabled": window_enabled,
        "daily_start": window_start,
        "daily_end": window_end,
        "camera_comment": comment or None,
    }


def entry_summary(entry: AccessListEntry) -> dict[str, object]:
    return {
        "plate": entry.vehicle.plate_text,
        "reference_name": entry.vehicle.reference_name,
        "site": entry.site.code,
        "list_type": entry.list_type,
        "enabled": entry.enabled,
        "valid_from": entry.valid_from.isoformat() if entry.valid_from else None,
        "valid_until": entry.valid_until.isoformat() if entry.valid_until else None,
        "daily_window_enabled": entry.daily_window_enabled,
        "daily_start": entry.daily_start.isoformat() if entry.daily_start else None,
        "daily_end": entry.daily_end.isoformat() if entry.daily_end else None,
        "camera_comment": entry.camera_comment,
    }


def overall_sync_status(entry: AccessListEntry) -> str:
    statuses = {item.status for item in entry.camera_syncs}
    if not statuses:
        return "NO CAMERAS"
    for priority in ("MISMATCH", "ERROR", "TIMEOUT_UNCERTAIN", "CAMERA_OFFLINE", "SYNCING", "PENDING", "PARTIALLY_VERIFIED"):
        if priority in statuses:
            return priority
    return "VERIFIED" if statuses == {"VERIFIED"} else sorted(statuses)[0]


def display_datetime(value: datetime | None, timezone_name: str) -> str:
    if value is None:
        return "Never"
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(zone).strftime("%d %b %Y %H:%M:%S")


def _local_datetime(value: object, timezone_name: str, label: str) -> datetime | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    try:
        parsed = datetime.fromisoformat(clean)
    except ValueError as error:
        raise AccessListValidationError(f"{label} is invalid") from error
    try:
        local_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        local_zone = timezone.utc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_zone)
    return parsed.astimezone(timezone.utc)


def _clock(value: object, label: str) -> time | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    try:
        return time.fromisoformat(clean)
    except ValueError as error:
        raise AccessListValidationError(f"{label} must use HH:MM or HH:MM:SS") from error
