from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.camera import Camera
from app.models.monitoring import ComponentHealth, OperationalAlert, UnifiedEvent
from app.models.site import Site


def _zone(name: str | None) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(name or "Asia/Kuala_Lumpur")
    except ZoneInfoNotFoundError:
        return timezone.utc


def _display(value: datetime | None, timezone_name: str | None = None) -> str:
    if value is None:
        return "Never"
    utc = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return utc.astimezone(_zone(timezone_name)).strftime("%d %b %Y %H:%M:%S")


def source_url(event: UnifiedEvent) -> str | None:
    if event.source_type == "RecognitionEvent":
        return f"/operations/events/{event.source_id}"
    if event.source_type == "VisitorReservation":
        return f"/residential/visitors/{event.source_id}"
    if event.source_type == "ParkingSession":
        return f"/parking/sessions/{event.source_id}"
    if event.source_type == "BarrierAction":
        return "/barrier-actions"
    if event.source_type == "HealthTransition":
        return "/system-health"
    return None


@dataclass(frozen=True)
class EventPage:
    items: tuple[UnifiedEvent, ...]
    page: int
    page_count: int
    total: int


class MonitoringQueryService:
    def __init__(self, db: Session):
        self.db = db

    def events(self, *, tab: str = "attention", page: int = 1, page_size: int = 50,
               category: str = "", event_type: str = "", severity: str = "",
               site_id: UUID | None = None, camera_id: UUID | None = None,
               search: str = "", operator: str = "", date_from: datetime | None = None,
               date_to: datetime | None = None, after: datetime | None = None) -> EventPage:
        query = select(UnifiedEvent).options(joinedload(UnifiedEvent.site), joinedload(UnifiedEvent.camera))
        count_query = select(func.count()).select_from(UnifiedEvent)
        criteria = []
        if tab == "attention":
            criteria.append(UnifiedEvent.attention_required.is_(True))
        if category:
            criteria.append(UnifiedEvent.category == category)
        if event_type:
            criteria.append(UnifiedEvent.event_type == event_type)
        if severity:
            criteria.append(UnifiedEvent.severity == severity)
        if site_id:
            criteria.append(UnifiedEvent.site_id == site_id)
        if camera_id:
            criteria.append(UnifiedEvent.camera_id == camera_id)
        if after:
            criteria.append(UnifiedEvent.occurred_at > after)
        if date_from:
            start = date_from.replace(tzinfo=_zone(None)) if date_from.tzinfo is None else date_from
            criteria.append(UnifiedEvent.occurred_at >= start.astimezone(timezone.utc))
        if date_to:
            end = date_to.replace(tzinfo=_zone(None)) if date_to.tzinfo is None else date_to
            criteria.append(UnifiedEvent.occurred_at <= end.astimezone(timezone.utc))
        if search.strip():
            pattern = f"%{search.strip()}%"
            criteria.append(or_(UnifiedEvent.display_subject.ilike(pattern), UnifiedEvent.summary.ilike(pattern), UnifiedEvent.event_type.ilike(pattern)))
        if operator.strip():
            criteria.append(UnifiedEvent.summary.ilike(f"%{operator.strip()}%"))
        if criteria:
            query = query.where(*criteria)
            count_query = count_query.where(*criteria)
        total = self.db.scalar(count_query) or 0
        page_count = max(1, math.ceil(total / page_size))
        page = min(max(1, page), page_count)
        items = tuple(self.db.scalars(query.order_by(UnifiedEvent.occurred_at.desc(), UnifiedEvent.id.desc())
            .offset((page - 1) * page_size).limit(page_size)).unique().all())
        return EventPage(items, page, page_count, total)

    def alerts(self, *, status: str = "active") -> tuple[OperationalAlert, ...]:
        query = select(OperationalAlert).options(joinedload(OperationalAlert.site), joinedload(OperationalAlert.acknowledged_by))
        if status == "active":
            query = query.where(OperationalAlert.status.in_(("ACTIVE", "ACKNOWLEDGED")))
        elif status == "resolved":
            query = query.where(OperationalAlert.status == "RESOLVED")
        return tuple(self.db.scalars(query.order_by(OperationalAlert.severity.asc(), OperationalAlert.last_seen_at.desc())).unique().all())

    def correlated(self, event: UnifiedEvent) -> tuple[UnifiedEvent, ...]:
        if event.correlation_id is None:
            return (event,)
        return tuple(self.db.scalars(select(UnifiedEvent).where(UnifiedEvent.correlation_id == event.correlation_id)
            .order_by(UnifiedEvent.occurred_at, UnifiedEvent.created_at)).all())

    def health_groups(self) -> dict[str, tuple[ComponentHealth, ...]]:
        group_map = {
            "APPLICATION": "Core Services", "DATABASE": "Core Services", "MQTT_BROKER": "Core Services", "MQTT_CLIENT": "Core Services", "RECORDER": "Core Services",
            "CAMERA": "Cameras", "MEDIA": "Media / Storage", "RECORDING_SOURCE": "Media / Storage", "STORAGE": "Media / Storage",
            "ACCESS_SYNC": "Access", "OFFLINE_RECOVERY": "Access",
            "BARRIER_CONTROL": "Lane Equipment", "BARRIER_FEEDBACK": "Lane Equipment", "PERIPHERAL": "Lane Equipment",
            "HOST": "Server",
        }
        groups: dict[str, list[ComponentHealth]] = {}
        rows = self.db.scalars(select(ComponentHealth).options(joinedload(ComponentHealth.site)).order_by(ComponentHealth.component_type, ComponentHealth.display_name)).unique().all()
        for item in rows:
            groups.setdefault(group_map.get(item.component_type, "Other"), []).append(item)
        return {name: tuple(items) for name, items in groups.items()}

    def health_counts(self) -> dict[str, int]:
        counts = {state: 0 for state in ("HEALTHY", "DEGRADED", "FAULT", "UNKNOWN", "UNVERIFIED", "DISABLED")}
        for state, count in self.db.execute(select(ComponentHealth.health_state, func.count()).group_by(ComponentHealth.health_state)):
            counts[state] = count
        return counts

    def site_health_summaries(self) -> tuple[dict[str, object], ...]:
        summaries: list[dict[str, object]] = []
        for site in self.sites():
            counts = {state: 0 for state in ("HEALTHY", "DEGRADED", "FAULT", "UNKNOWN", "UNVERIFIED", "DISABLED")}
            for state, count in self.db.execute(select(ComponentHealth.health_state, func.count()).where(
                ComponentHealth.site_id == site.id).group_by(ComponentHealth.health_state)):
                counts[state] = count
            overall = "FAULT" if counts["FAULT"] else "DEGRADED" if counts["DEGRADED"] else "HEALTHY" if counts["HEALTHY"] else "UNKNOWN"
            summaries.append({"site": site, "counts": counts, "overall": overall})
        return tuple(summaries)

    def event_types(self) -> tuple[str, ...]:
        return tuple(self.db.scalars(select(UnifiedEvent.event_type).distinct().order_by(UnifiedEvent.event_type)).all())

    def sites(self) -> tuple[Site, ...]:
        return tuple(self.db.scalars(select(Site).order_by(Site.code)).all())

    def cameras(self) -> tuple[Camera, ...]:
        return tuple(self.db.scalars(select(Camera).order_by(Camera.code)).all())

    @staticmethod
    def display_time(value: datetime | None, timezone_name: str | None = None) -> str:
        return _display(value, timezone_name)

    @staticmethod
    def source_url(event: UnifiedEvent) -> str | None:
        return source_url(event)
