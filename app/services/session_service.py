from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import generate_session_token, hash_session_token
from app.models.user import User
from app.models.user_session import UserSession


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def create_session(
    db: Session,
    user: User,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> tuple[str, UserSession]:
    """Create a server-side session and return its raw token exactly once."""
    issued_at = _as_utc(now) if now is not None else _utc_now()
    raw_token = generate_session_token()
    user_session = UserSession(
        user_id=user.id,
        token_hash=hash_session_token(raw_token),
        ip_address=ip_address[:45] if ip_address else None,
        user_agent=user_agent[:500] if user_agent else None,
        created_at=issued_at,
        last_seen_at=issued_at,
        expires_at=issued_at
        + timedelta(minutes=settings.session_lifetime_minutes),
    )
    db.add(user_session)
    db.flush()
    return raw_token, user_session


def find_valid_session(
    db: Session,
    raw_session_token: str,
    *,
    now: datetime | None = None,
    touch: bool = True,
) -> UserSession | None:
    """Find a non-revoked session within its absolute and idle lifetimes."""
    if not raw_session_token:
        return None

    checked_at = _as_utc(now) if now is not None else _utc_now()
    user_session = db.scalar(
        select(UserSession).where(
            UserSession.token_hash == hash_session_token(raw_session_token)
        )
    )

    if user_session is None or user_session.revoked_at is not None:
        return None

    if _as_utc(user_session.expires_at) <= checked_at:
        return None

    idle_deadline = _as_utc(user_session.last_seen_at) + timedelta(
        minutes=settings.session_idle_minutes
    )
    if idle_deadline <= checked_at:
        return None

    if touch:
        user_session.last_seen_at = checked_at
        db.flush()

    return user_session


def revoke_session(
    db: Session,
    raw_session_token: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Revoke a session identified by its raw browser token."""
    if not raw_session_token:
        return False

    user_session = db.scalar(
        select(UserSession).where(
            UserSession.token_hash == hash_session_token(raw_session_token)
        )
    )
    if user_session is None:
        return False

    if user_session.revoked_at is None:
        user_session.revoked_at = _as_utc(now) if now is not None else _utc_now()
        db.flush()

    return True


def revoke_all_user_sessions(
    db: Session,
    user_id: UUID,
    *,
    now: datetime | None = None,
) -> int:
    """Revoke every currently active session belonging to a user."""
    revoked_at = _as_utc(now) if now is not None else _utc_now()
    result = db.execute(
        update(UserSession)
        .where(
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
        )
        .values(revoked_at=revoked_at)
    )
    db.flush()
    return result.rowcount or 0
