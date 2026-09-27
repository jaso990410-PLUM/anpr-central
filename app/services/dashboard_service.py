from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.camera import Camera
from app.models.site import Site
from app.models.user import User
from app.models.user_session import UserSession


@dataclass(frozen=True)
class DashboardSummary:
    site_count: int
    active_site_count: int
    primary_site_code: str | None
    camera_count: int
    enabled_camera_count: int
    user_count: int
    active_user_count: int
    active_session_count: int

    @property
    def site_context(self) -> str:
        if self.active_site_count == 1 and self.primary_site_code:
            return self.primary_site_code
        if self.active_site_count:
            return f"{self.active_site_count} ACTIVE SITES"
        return "NO ACTIVE SITE"


def get_dashboard_summary(
    db: Session,
    *,
    now: datetime | None = None,
) -> DashboardSummary:
    """Return truthful operational counts for the dashboard shell."""
    checked_at = now or datetime.now(timezone.utc)
    idle_cutoff = checked_at - timedelta(minutes=settings.session_idle_minutes)

    site_count = db.scalar(select(func.count()).select_from(Site)) or 0
    active_site_count = db.scalar(
        select(func.count()).select_from(Site).where(Site.is_active.is_(True))
    ) or 0
    primary_site_code = db.scalar(
        select(Site.code)
        .where(Site.is_active.is_(True))
        .order_by(Site.code)
        .limit(1)
    )

    camera_count = db.scalar(select(func.count()).select_from(Camera)) or 0
    enabled_camera_count = db.scalar(
        select(func.count())
        .select_from(Camera)
        .where(Camera.is_enabled.is_(True))
    ) or 0

    user_count = db.scalar(select(func.count()).select_from(User)) or 0
    active_user_count = db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.is_active.is_(True))
    ) or 0
    active_session_count = db.scalar(
        select(func.count(distinct(UserSession.user_id)))
        .select_from(UserSession)
        .join(User, User.id == UserSession.user_id)
        .where(
            User.is_active.is_(True),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > checked_at,
            UserSession.last_seen_at > idle_cutoff,
        )
    ) or 0

    return DashboardSummary(
        site_count=site_count,
        active_site_count=active_site_count,
        primary_site_code=primary_site_code,
        camera_count=camera_count,
        enabled_camera_count=enabled_camera_count,
        user_count=user_count,
        active_user_count=active_user_count,
        active_session_count=active_session_count,
    )
