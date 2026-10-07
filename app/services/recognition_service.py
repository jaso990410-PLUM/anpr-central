import base64
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
import math
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.models.access_list_entry import AccessListEntry
from app.models.camera import Camera
from app.models.recognition_event import RecognitionEvent
from app.models.site import Site
from app.models.vehicle import Vehicle
from app.services.access_list_service import overall_sync_status


class LiveCursorError(ValueError):
    pass


@dataclass(frozen=True)
class RecognitionPage:
    items: tuple[dict[str, object], ...]
    page: int
    page_count: int
    total: int


class RecognitionService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def history(
        self,
        *,
        plate: str | None = None,
        camera_id: UUID | None = None,
        direction: str | None = None,
        source: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        page: int = 1,
    ) -> RecognitionPage:
        conditions = self._conditions(
            plate=plate,
            camera_id=camera_id,
            direction=direction,
            source=source,
            date_from=date_from,
            date_to=date_to,
        )
        total = self.db.scalar(
            select(func.count())
            .select_from(RecognitionEvent)
            .join(Camera, Camera.id == RecognitionEvent.camera_id)
            .where(*conditions)
        ) or 0
        page_count = max(1, math.ceil(total / settings.recognition_page_size))
        page = min(max(1, page), page_count)
        rows = self.db.execute(
            self._event_query()
            .where(*conditions)
            .order_by(
                func.coalesce(
                    RecognitionEvent.captured_at, RecognitionEvent.received_at
                ).desc(),
                RecognitionEvent.id.desc(),
            )
            .offset((page - 1) * settings.recognition_page_size)
            .limit(settings.recognition_page_size)
        ).all()
        return RecognitionPage(
            items=tuple(_event_dict(*row) for row in rows),
            page=page,
            page_count=page_count,
            total=total,
        )

    def latest(self) -> dict[str, object] | None:
        row = self.db.execute(
            self._event_query()
            .order_by(
                func.coalesce(
                    RecognitionEvent.captured_at, RecognitionEvent.received_at
                ).desc(),
                RecognitionEvent.received_at.desc(),
                RecognitionEvent.id.desc(),
            )
            .limit(1)
        ).one_or_none()
        return _event_dict(*row) if row else None

    def current_cursor(self, *, source: str | None = None) -> str | None:
        query = select(RecognitionEvent.updated_at, RecognitionEvent.id)
        clean_source = (source or "").strip().upper()
        if clean_source in {"LIVE", "OFFLINE_RECOVERY"}:
            query = query.where(RecognitionEvent.source == clean_source)
        row = self.db.execute(
            query
            .order_by(
                RecognitionEvent.updated_at.desc(), RecognitionEvent.id.desc()
            )
            .limit(1)
        ).one_or_none()
        return encode_cursor(*row) if row else None

    def changed_after(
        self, cursor: str | None, *, limit: int = 50, source: str | None = None
    ) -> tuple[tuple[dict[str, object], ...], str | None]:
        limit = min(max(1, limit), 100)
        query = self._event_query()
        clean_source = (source or "").strip().upper()
        if clean_source in {"LIVE", "OFFLINE_RECOVERY"}:
            query = query.where(RecognitionEvent.source == clean_source)
        if cursor:
            changed_at, event_id = decode_cursor(cursor)
            query = query.where(
                or_(
                    RecognitionEvent.updated_at > changed_at,
                    and_(
                        RecognitionEvent.updated_at == changed_at,
                        RecognitionEvent.id > event_id,
                    ),
                )
            )
        rows = self.db.execute(
            query.order_by(
                RecognitionEvent.updated_at, RecognitionEvent.id
            ).limit(limit)
        ).all()
        items = tuple(_event_dict(*row) for row in rows)
        next_cursor = (
            encode_cursor(rows[-1][0].updated_at, rows[-1][0].id)
            if rows
            else cursor or self.current_cursor(source=source)
        )
        return items, next_cursor

    def recent_access(self, *, limit: int = 100) -> tuple[dict[str, object], ...]:
        rows = self.db.execute(
            self._event_query()
            .order_by(RecognitionEvent.received_at.desc(), RecognitionEvent.id.desc())
            .limit(min(max(1, limit), 100))
        ).all()
        return tuple(
            {
                "id": str(event.id),
                **_access_dict(entry, site),
            }
            for event, _camera, site, entry in rows
        )

    @staticmethod
    def _event_query():
        return (
            select(RecognitionEvent, Camera, Site, AccessListEntry)
            .join(Camera, Camera.id == RecognitionEvent.camera_id)
            .join(Site, Site.id == RecognitionEvent.site_id)
            .outerjoin(
                Vehicle,
                Vehicle.plate_text == func.upper(func.trim(RecognitionEvent.plate_text)),
            )
            .outerjoin(
                AccessListEntry,
                and_(
                    AccessListEntry.vehicle_id == Vehicle.id,
                    AccessListEntry.site_id == RecognitionEvent.site_id,
                ),
            )
            .options(selectinload(AccessListEntry.camera_syncs))
        )

    def _conditions(
        self,
        *,
        plate: str | None,
        camera_id: UUID | None,
        direction: str | None,
        source: str | None,
        date_from: date | None,
        date_to: date | None,
    ) -> tuple[object, ...]:
        conditions = []
        clean_plate = (plate or "").strip().upper()
        if clean_plate:
            conditions.append(RecognitionEvent.plate_text.ilike(f"%{clean_plate}%"))
        if camera_id is not None:
            conditions.append(RecognitionEvent.camera_id == camera_id)
        clean_direction = (direction or "").strip().upper()
        if clean_direction:
            conditions.append(
                or_(
                    RecognitionEvent.direction == clean_direction,
                    and_(
                        or_(
                            RecognitionEvent.direction.is_(None),
                            ~RecognitionEvent.direction.in_(("ENTRY", "EXIT")),
                        ),
                        func.upper(Camera.direction) == clean_direction,
                    ),
                )
            )
        clean_source = (source or "").strip().upper()
        if clean_source in {"LIVE", "OFFLINE_RECOVERY"}:
            conditions.append(RecognitionEvent.source == clean_source)
        display_timezone = self._timezone()
        if date_from:
            start = datetime.combine(date_from, time.min, display_timezone)
            conditions.append(
                func.coalesce(RecognitionEvent.captured_at, RecognitionEvent.received_at)
                >= start.astimezone(timezone.utc)
            )
        if date_to:
            end = datetime.combine(date_to + timedelta(days=1), time.min, display_timezone)
            conditions.append(
                func.coalesce(RecognitionEvent.captured_at, RecognitionEvent.received_at)
                < end.astimezone(timezone.utc)
            )
        return tuple(conditions)

    def _timezone(self) -> ZoneInfo | timezone:
        name = self.db.scalar(
            select(Site.timezone).order_by(Site.is_active.desc(), Site.code).limit(1)
        ) or "Asia/Kuala_Lumpur"
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return timezone.utc


def encode_cursor(changed_at: datetime, event_id: UUID) -> str:
    payload = json.dumps(
        {"t": _utc(changed_at).isoformat(), "i": str(event_id)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    if not cursor or len(cursor) > 300:
        raise LiveCursorError("Invalid live cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        if set(value) != {"t", "i"}:
            raise ValueError
        changed_at = datetime.fromisoformat(value["t"])
        event_id = UUID(value["i"])
        if changed_at.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise LiveCursorError("Invalid live cursor") from error
    return changed_at.astimezone(timezone.utc), event_id


def _event_dict(
    event: RecognitionEvent,
    camera: Camera,
    site: Site,
    access_entry: AccessListEntry | None,
) -> dict[str, object]:
    timestamp = event.captured_at or event.received_at
    try:
        display_timezone = ZoneInfo(site.timezone)
    except ZoneInfoNotFoundError:
        display_timezone = timezone.utc
    return {
        "id": str(event.id),
        "plate": event.plate_text,
        "camera": camera.code,
        "camera_name": camera.name,
        "site": site.code,
        "direction": _operator_direction(event.direction, camera.direction).title(),
        "direction_code": _operator_direction(event.direction, camera.direction),
        "confidence": event.confidence,
        "confidence_display": (
            f"{event.confidence:g}%" if event.confidence is not None else "—"
        ),
        "captured_at": _utc(timestamp).isoformat(),
        "captured_display": _utc(timestamp).astimezone(display_timezone).strftime(
            "%d %b %Y %H:%M:%S"
        ),
        "captured_time_display": _utc(timestamp).astimezone(display_timezone).strftime(
            "%H:%M:%S"
        ),
        "status": "Processing" if event.event_type == "QUICK" else "Complete",
        "source": event.source,
        "source_label": "Recovered" if event.source == "OFFLINE_RECOVERY" else "Live",
        "has_scene_image": bool(event.scene_image_path),
        "has_plate_image": bool(event.plate_image_path),
        "scene_image_url": (
            f"/recognition/{event.id}/image/scene" if event.scene_image_path else None
        ),
        "plate_image_url": (
            f"/recognition/{event.id}/image/plate" if event.plate_image_path else None
        ),
        "updated_at": _utc(event.updated_at).isoformat(),
        **_access_dict(access_entry, site),
    }


def _access_dict(
    entry: AccessListEntry | None,
    site: Site,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    if entry is None:
        return {
            "access_status": "NOT_LISTED",
            "access_label": "Not Listed",
            "access_badge_tone": "neutral",
            "access_entry_id": None,
            "access_sync_status": None,
        }

    classification = "Whitelist" if entry.list_type == "ALLOW" else "Blacklist"
    prefix = classification.upper()
    current = _utc(now or datetime.now(timezone.utc))
    status = prefix
    suffix = ""
    tone = "online" if entry.list_type == "ALLOW" else "critical"
    if not entry.enabled:
        status = f"{prefix}_DISABLED"
        suffix = "Disabled"
        tone = "disabled"
    elif entry.valid_from and current < _utc(entry.valid_from):
        status = f"{prefix}_NOT_YET_VALID"
        suffix = "Not Yet Valid"
        tone = "warning"
    elif entry.valid_until and current >= _utc(entry.valid_until):
        status = f"{prefix}_EXPIRED"
        suffix = "Expired"
        tone = "warning"
    elif entry.daily_window_enabled and entry.daily_start and entry.daily_end:
        try:
            local_time = current.astimezone(ZoneInfo(site.timezone)).time().replace(tzinfo=None)
        except ZoneInfoNotFoundError:
            local_time = current.time().replace(tzinfo=None)
        start, end = entry.daily_start, entry.daily_end
        within_window = start <= local_time < end if start < end else local_time >= start or local_time < end
        if not within_window:
            status = f"{prefix}_OUTSIDE_HOURS"
            suffix = "Outside Hours"
            tone = "warning"

    return {
        "access_status": status,
        "access_label": f"{classification} · {suffix}" if suffix else classification,
        "access_badge_tone": tone,
        "access_entry_id": str(entry.id),
        "access_sync_status": overall_sync_status(entry).replace("_", " ").title(),
    }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _operator_direction(event_direction: str | None, camera_direction: str | None) -> str:
    event_value = (event_direction or "").strip().upper()
    if event_value in {"ENTRY", "EXIT"}:
        return event_value
    camera_value = (camera_direction or "").strip().upper()
    return camera_value if camera_value else "UNKNOWN"
