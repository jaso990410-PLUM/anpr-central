from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.camera import Camera
from app.models.monitoring import ComponentHealth, OperationalAlert, UnifiedEvent
from app.models.site import Site


CATEGORY_LABELS = {
    "TRAFFIC_ANPR": "Traffic / ANPR",
    "ACCESS_SECURITY": "Access & Security",
    "ADMIN_BUSINESS": "Administration",
    "OPERATOR_ACTION": "Operator Actions",
    "SYSTEM_HEALTH": "System Health",
}

CATEGORY_ICONS = {
    "TRAFFIC_ANPR": "ANPR",
    "ACCESS_SECURITY": "SEC",
    "ADMIN_BUSINESS": "ADM",
    "OPERATOR_ACTION": "OPS",
    "SYSTEM_HEALTH": "SYS",
}

ROUTINE_RECOGNITION_TYPES = {"ANPR_RECOGNITION", "OFFLINE_RECOGNITION_RECOVERED"}


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

    def event_center_summary(self) -> dict[str, int]:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        active_alerts = self.db.scalar(select(func.count()).select_from(OperationalAlert).where(
            OperationalAlert.status.in_(("ACTIVE", "ACKNOWLEDGED")))) or 0
        attention = self.db.scalar(select(func.count()).select_from(UnifiedEvent).where(
            UnifiedEvent.attention_required.is_(True), UnifiedEvent.occurred_at >= since)) or 0
        critical = self.db.scalar(select(func.count()).select_from(UnifiedEvent).where(
            UnifiedEvent.severity == "CRITICAL", UnifiedEvent.occurred_at >= since)) or 0
        operational = self.db.scalar(select(func.count()).select_from(UnifiedEvent).where(
            UnifiedEvent.occurred_at >= since,
            UnifiedEvent.event_type.not_in(ROUTINE_RECOGNITION_TYPES))) or 0
        return {"active_alerts": active_alerts, "attention": attention,
                "critical": critical, "last_24h": operational}

    def correlation_counts(self, events: tuple[UnifiedEvent, ...]) -> dict[UUID, int]:
        correlation_ids = {event.correlation_id for event in events if event.correlation_id is not None}
        if not correlation_ids:
            return {}
        return {correlation_id: count for correlation_id, count in self.db.execute(
            select(UnifiedEvent.correlation_id, func.count())
            .where(UnifiedEvent.correlation_id.in_(correlation_ids))
            .group_by(UnifiedEvent.correlation_id)
        ) if count > 1}

    def alerts_for_events(self, events: tuple[UnifiedEvent, ...]) -> dict[UUID, OperationalAlert]:
        event_ids = {event.id for event in events}
        if not event_ids:
            return {}
        rows = self.db.scalars(select(OperationalAlert).where(or_(
            OperationalAlert.trigger_event_id.in_(event_ids),
            OperationalAlert.resolution_event_id.in_(event_ids),
        )).order_by(OperationalAlert.last_seen_at.desc())).all()
        result: dict[UUID, OperationalAlert] = {}
        for alert in rows:
            if alert.trigger_event_id in event_ids:
                result.setdefault(alert.trigger_event_id, alert)
            if alert.resolution_event_id in event_ids:
                result.setdefault(alert.resolution_event_id, alert)
        return result

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

    @staticmethod
    def alert_map(alerts: tuple[OperationalAlert, ...]) -> dict[str, OperationalAlert]:
        return {f"{alert.component_type}:{alert.component_id}": alert for alert in alerts}

    @staticmethod
    def relative_time(value: datetime | None, *, now: datetime | None = None) -> str:
        if value is None:
            return "Never"
        current = now or datetime.now(timezone.utc)
        observed = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        seconds = max(0, int((current - observed).total_seconds()))
        if seconds < 10:
            return "just now"
        if seconds < 60:
            return f"{seconds} sec ago"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} min ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} hr ago"
        days = hours // 24
        return f"{days} day{'s' if days != 1 else ''} ago"

    @staticmethod
    def state_age(item: ComponentHealth, *, now: datetime | None = None) -> str:
        age = MonitoringQueryService.relative_time(item.state_changed_at, now=now)
        return "Changed just now" if age == "just now" else f"{item.health_state.title()} for {age.removesuffix(' ago')}"

    @staticmethod
    def component_summary(item: ComponentHealth) -> str:
        capabilities = item.capabilities or {}
        if item.component_type == "MQTT_CLIENT" and item.health_state == "UNKNOWN":
            return "Runtime health cannot currently be independently verified."
        if item.component_type == "MEDIA" and item.health_state in {"DEGRADED", "UNKNOWN"}:
            return "Live stream has not been verified by the monitor. Recording health is tracked separately."
        if item.component_type == "STORAGE" and capabilities.get("free_bytes") is not None:
            free_gb = capabilities["free_bytes"] / (1024 ** 3)
            writable = "Recording writable" if capabilities.get("writable") else "Not writable"
            return f"{free_gb:,.0f} GB free · {capabilities.get('free_percent', 0):.1f}% · {writable}"
        if item.component_type == "RECORDING_SOURCE" and item.health_state in {"FAULT", "DEGRADED"}:
            last_segment = capabilities.get("last_segment_at")
            segment_text = "No completed recording segment is available"
            if last_segment:
                try:
                    segment_text = f"Last completed segment {MonitoringQueryService.relative_time(datetime.fromisoformat(last_segment))}"
                except (TypeError, ValueError):
                    pass
            recorder_state = capabilities.get("recorder_state", "unknown")
            return f"Recording is stale. {segment_text}. Recorder reports {recorder_state}."
        if item.component_type == "CAMERA" and item.health_state == "FAULT":
            last_seen = capabilities.get("last_seen_at")
            if last_seen:
                try:
                    return f"Camera heartbeat is stale · last seen {MonitoringQueryService.relative_time(datetime.fromisoformat(last_seen))}."
                except (TypeError, ValueError):
                    pass
        return item.summary

    @staticmethod
    def component_action(item: ComponentHealth) -> str:
        if item.health_state in {"FAULT", "DEGRADED"}:
            if item.component_type == "STORAGE":
                return "Open Storage"
            if item.component_type == "ACCESS_SYNC":
                return "View Sync Status"
            return "Investigate"
        return "Details"

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
    def category_label(category: str) -> str:
        return CATEGORY_LABELS.get(category, category.replace("_", " ").title())

    @staticmethod
    def category_icon(category: str) -> str:
        return CATEGORY_ICONS.get(category, "EVT")

    @staticmethod
    def date_group(event: UnifiedEvent) -> str:
        zone = _zone(event.site.timezone if event.site else None)
        local_day = event.occurred_at.replace(tzinfo=timezone.utc).astimezone(zone).date() if event.occurred_at.tzinfo is None else event.occurred_at.astimezone(zone).date()
        today = datetime.now(timezone.utc).astimezone(zone).date()
        if local_day == today:
            return "Today"
        if local_day == today - timedelta(days=1):
            return "Yesterday"
        return local_day.strftime("%d %b %Y")

    @staticmethod
    def event_context(event: UnifiedEvent) -> tuple[str, ...]:
        context: list[str] = []
        if event.site:
            context.append(event.site.code)
        if event.camera:
            context.append(event.camera.code)
        if not context:
            context.append("System-wide")
        return tuple(context)

    @staticmethod
    def source_action(event: UnifiedEvent) -> str:
        if event.source_type == "RecognitionEvent":
            return "View Recognition"
        if event.source_type == "HealthTransition":
            return "Investigate"
        if event.source_type == "BarrierAction":
            return "View Barrier Action"
        return "Open Source"

    @staticmethod
    def is_major(event: UnifiedEvent) -> bool:
        return event.severity == "CRITICAL" or event.event_type in {
            "BLACKLIST_MATCH", "WATCHLIST_MATCH", "CAMERA_OFFLINE", "RECORDING_GAP",
            "STORAGE_FAULT", "MQTT_DISCONNECTED", "BARRIER_COMMAND_FAILED",
        }

    @staticmethod
    def event_video_url(event: UnifiedEvent) -> str | None:
        if event.correlation_id and event.category in {"TRAFFIC_ANPR", "ACCESS_SECURITY"}:
            return f"/media/events/{event.correlation_id}"
        if event.source_type == "RecognitionEvent":
            return f"/media/events/{event.source_id}"
        return None

    @staticmethod
    def display_time(value: datetime | None, timezone_name: str | None = None) -> str:
        return _display(value, timezone_name)

    @staticmethod
    def source_url(event: UnifiedEvent) -> str | None:
        return source_url(event)
