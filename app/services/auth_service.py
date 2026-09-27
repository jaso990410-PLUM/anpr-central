from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.models.user import User


INVALID_CREDENTIALS_MESSAGE = "Invalid username or password"
_DUMMY_PASSWORD_HASH = hash_password("dummy-password-never-used-for-login")


class AuthenticationError(ValueError):
    """A deliberately generic authentication failure."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _reject_login() -> None:
    raise AuthenticationError(INVALID_CREDENTIALS_MESSAGE)


def authenticate_user(
    db: Session,
    username: str,
    password: str,
    *,
    now: datetime | None = None,
) -> User:
    """Authenticate a user while applying inactive and lockout policies."""
    attempted_at = _as_utc(now) if now is not None else _utc_now()
    user = db.scalar(select(User).where(User.username == username.strip()))

    if user is None:
        verify_password(password, _DUMMY_PASSWORD_HASH)
        _reject_login()

    password_is_valid = verify_password(password, user.password_hash)

    if not user.is_active:
        _reject_login()

    if user.locked_until is not None:
        if _as_utc(user.locked_until) > attempted_at:
            _reject_login()

        # A completed lockout period starts a fresh sequence of attempts.
        user.failed_login_count = 0
        user.locked_until = None

    if not password_is_valid:
        user.failed_login_count += 1
        if user.failed_login_count >= settings.login_max_failures:
            user.locked_until = attempted_at + timedelta(
                minutes=settings.login_lock_minutes
            )
        db.flush()
        _reject_login()

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = attempted_at
    db.flush()
    return user
