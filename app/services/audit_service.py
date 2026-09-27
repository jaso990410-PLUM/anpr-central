from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.site import Site
from app.models.user import User


_SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "authorization",
    "cookie",
    "credential",
    "hash",
    "csrf",
)


def _safe_details(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _safe_details(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in _SENSITIVE_KEY_PARTS)
        }
    if isinstance(value, (list, tuple)):
        return [_safe_details(item) for item in value]
    return value


def create_audit_log(
    db: Session,
    *,
    action: str,
    actor_user_id: UUID | None = None,
    site_id: UUID | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    details: Mapping[str, Any] | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    """Stage an audit event, removing credential-like detail fields."""
    audit_log = AuditLog(
        actor_user_id=actor_user_id,
        site_id=site_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=_safe_details(details) if details is not None else None,
        ip_address=ip_address,
    )
    db.add(audit_log)
    db.flush()
    return audit_log


@dataclass(frozen=True)
class AuditActorOption:
    id: UUID
    username: str
    full_name: str


@dataclass(frozen=True)
class AuditListItem:
    timestamp_display: str
    actor_display: str
    action: str
    entity_type: str | None
    entity_id: str | None
    ip_address: str | None
    details_display: str | None


class AuditService:
    def __init__(self, db: Session):
        self.db = db

    def list_entries(
        self,
        *,
        action: str | None = None,
        actor: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> tuple[AuditListItem, ...]:
        timezone_name = self._display_timezone()
        display_timezone = _timezone(timezone_name)
        query = (
            select(AuditLog, User.username, User.full_name)
            .outerjoin(User, User.id == AuditLog.actor_user_id)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        )

        clean_action = (action or "").strip()
        if clean_action:
            query = query.where(AuditLog.action == clean_action)

        clean_actor = (actor or "").strip()
        if clean_actor:
            try:
                actor_id = UUID(clean_actor)
            except ValueError:
                actor_pattern = f"%{clean_actor}%"
                query = query.where(
                    or_(
                        User.username.ilike(actor_pattern),
                        User.full_name.ilike(actor_pattern),
                        User.email.ilike(actor_pattern),
                    )
                )
            else:
                query = query.where(AuditLog.actor_user_id == actor_id)

        if date_from is not None:
            start = datetime.combine(date_from, time.min, display_timezone)
            query = query.where(AuditLog.created_at >= start.astimezone(timezone.utc))
        if date_to is not None:
            end = datetime.combine(
                date_to + timedelta(days=1),
                time.min,
                display_timezone,
            )
            query = query.where(AuditLog.created_at < end.astimezone(timezone.utc))

        return tuple(
            AuditListItem(
                timestamp_display=_display_time(entry.created_at, display_timezone),
                actor_display=(
                    f"{username} · {full_name}"
                    if username is not None and full_name is not None
                    else "System"
                ),
                action=entry.action,
                entity_type=entry.entity_type,
                entity_id=entry.entity_id,
                ip_address=entry.ip_address,
                details_display=_format_details(entry.details),
            )
            for entry, username, full_name in self.db.execute(query).all()
        )

    def list_actions(self) -> tuple[str, ...]:
        return tuple(
            self.db.scalars(
                select(AuditLog.action).distinct().order_by(AuditLog.action)
            ).all()
        )

    def list_actors(self) -> tuple[AuditActorOption, ...]:
        rows = self.db.execute(
            select(User.id, User.username, User.full_name)
            .join(AuditLog, AuditLog.actor_user_id == User.id)
            .distinct()
            .order_by(User.username)
        ).all()
        return tuple(
            AuditActorOption(id=user_id, username=username, full_name=full_name)
            for user_id, username, full_name in rows
        )

    def _display_timezone(self) -> str:
        timezone_name = self.db.scalar(
            select(Site.timezone).where(Site.code == settings.default_site_code)
        )
        if timezone_name is None:
            timezone_name = self.db.scalar(
                select(Site.timezone)
                .order_by(Site.is_active.desc(), Site.code)
                .limit(1)
            )
        return timezone_name or "Asia/Kuala_Lumpur"


def _timezone(timezone_name: str) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def _display_time(value: datetime, display_timezone: ZoneInfo | timezone) -> str:
    utc_value = (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
    return utc_value.astimezone(display_timezone).strftime("%Y-%m-%d %H:%M:%S")


def _format_details(details: Mapping[str, Any] | None) -> str | None:
    safe_details = _safe_details(details) if details is not None else None
    if not safe_details:
        return None
    parts = [
        f"{key}={_format_detail_value(value)}"
        for key, value in safe_details.items()
    ]
    summary = "; ".join(parts)
    return summary if len(summary) <= 240 else f"{summary[:237]}..."


def _format_detail_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return str(value)
