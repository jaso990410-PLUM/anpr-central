from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.camera import Camera
from app.models.monitoring import ComponentHealth, OperationalAlert
from app.models.site import Site
from app.models.user import User
from app.models.user_session import UserSession
from app.services.camera_service import connection_state


@dataclass(frozen=True)
class SiteDashboardInfo:
    code: str
    name: str
    timezone: str
    is_active: bool


@dataclass(frozen=True)
class CameraDashboardInfo:
    id: UUID
    code: str
    name: str
    site_code: str
    direction: str
    ip_address: str | None
    is_enabled: bool
    last_seen_display: str | None
    connection_state: str


@dataclass(frozen=True)
class DashboardSummary:
    application_name: str
    application_version: str
    database_connected: bool
    site: SiteDashboardInfo | None
    site_count: int
    active_site_count: int
    camera_count: int
    enabled_camera_count: int
    online_camera_count: int
    cameras: tuple[CameraDashboardInfo, ...]
    user_count: int
    active_user_count: int
    active_session_count: int
    health_state: str
    health_fault_count: int
    health_degraded_count: int
    active_alert_count: int

    @property
    def site_context(self) -> str:
        return self.site.code if self.site is not None else "NO ACTIVE SITE"


def _current_site(db: Session) -> Site | None:
    site = db.scalar(
        select(Site).where(Site.code == settings.default_site_code)
    )
    if site is not None:
        return site

    return db.scalar(
        select(Site)
        .order_by(Site.is_active.desc(), Site.code)
        .limit(1)
    )


def _display_time(value: datetime | None, timezone_name: str) -> str | None:
    if value is None:
        return None

    utc_value = (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
    try:
        display_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        display_timezone = timezone.utc

    return utc_value.astimezone(display_timezone).strftime("%Y-%m-%d %H:%M:%S")


def get_dashboard_summary(
    db: Session,
    *,
    now: datetime | None = None,
) -> DashboardSummary:
    """Return database-backed information for the operations dashboard."""
    checked_at = now or datetime.now(timezone.utc)
    idle_cutoff = checked_at - timedelta(minutes=settings.session_idle_minutes)
    current_site = _current_site(db)

    database_connected = db.scalar(text("SELECT 1")) == 1
    site_count = db.scalar(select(func.count()).select_from(Site)) or 0
    active_site_count = db.scalar(
        select(func.count()).select_from(Site).where(Site.is_active.is_(True))
    ) or 0
    camera_count = db.scalar(select(func.count()).select_from(Camera)) or 0
    enabled_camera_count = db.scalar(
        select(func.count())
        .select_from(Camera)
        .where(Camera.is_enabled.is_(True))
    ) or 0
    online_camera_count = db.scalar(
        select(func.count())
        .select_from(Camera)
        .where(Camera.connection_state == "ONLINE")
    ) or 0
    user_count = db.scalar(select(func.count()).select_from(User)) or 0
    active_user_count = db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.is_active.is_(True))
    ) or 0
    active_session_count = db.scalar(
        select(func.count(UserSession.id))
        .select_from(UserSession)
        .join(User, User.id == UserSession.user_id)
        .where(
            User.is_active.is_(True),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > checked_at,
            UserSession.last_seen_at > idle_cutoff,
        )
    ) or 0
    health_observation_count = db.scalar(select(func.count()).select_from(ComponentHealth)) or 0
    last_health_observation = db.scalar(select(func.max(ComponentHealth.observed_at)))
    observation_utc = (
        last_health_observation.replace(tzinfo=timezone.utc)
        if last_health_observation is not None and last_health_observation.tzinfo is None
        else last_health_observation.astimezone(timezone.utc) if last_health_observation is not None else None
    )
    monitoring_stale = bool(
        observation_utc is not None
        and checked_at - observation_utc > timedelta(seconds=settings.monitor_stale_after_seconds)
    )
    health_fault_count = db.scalar(
        select(func.count()).select_from(ComponentHealth)
        .where(ComponentHealth.health_state == "FAULT")
    ) or 0
    health_degraded_count = db.scalar(
        select(func.count()).select_from(ComponentHealth)
        .where(ComponentHealth.health_state == "DEGRADED")
    ) or 0
    active_alert_count = db.scalar(
        select(func.count()).select_from(OperationalAlert)
        .where(OperationalAlert.status.in_(("ACTIVE", "ACKNOWLEDGED")))
    ) or 0
    health_state = (
        "FAULT" if not database_connected
        else "STALE" if monitoring_stale
        else "FAULT" if health_fault_count
        else "DEGRADED" if health_degraded_count or active_alert_count
        else "UNKNOWN" if not health_observation_count
        else "HEALTHY"
    )

    camera_rows = db.execute(
        select(Camera, Site.code, Site.timezone)
        .join(Site, Site.id == Camera.site_id)
        .order_by(Site.code, Camera.code)
    ).all()
    cameras = tuple(
        CameraDashboardInfo(
            id=camera.id,
            code=camera.code,
            name=camera.name,
            site_code=site_code,
            direction=camera.direction,
            ip_address=camera.ip_address,
            is_enabled=camera.is_enabled,
            last_seen_display=_display_time(camera.last_seen_at, site_timezone),
            connection_state=connection_state(camera),
        )
        for camera, site_code, site_timezone in camera_rows
    )

    site_info = (
        SiteDashboardInfo(
            code=current_site.code,
            name=current_site.name,
            timezone=current_site.timezone,
            is_active=current_site.is_active,
        )
        if current_site is not None
        else None
    )

    return DashboardSummary(
        application_name=settings.app_name,
        application_version=settings.app_version,
        database_connected=database_connected,
        site=site_info,
        site_count=site_count,
        active_site_count=active_site_count,
        camera_count=camera_count,
        enabled_camera_count=enabled_camera_count,
        online_camera_count=online_camera_count,
        cameras=cameras,
        user_count=user_count,
        active_user_count=active_user_count,
        active_session_count=active_session_count,
        health_state=health_state,
        health_fault_count=health_fault_count,
        health_degraded_count=health_degraded_count,
        active_alert_count=active_alert_count,
    )
