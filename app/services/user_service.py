from dataclasses import dataclass
from datetime import datetime, timezone
import re
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.security import hash_password
from app.models.role import Role
from app.models.site import Site
from app.models.user import User


_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserValidationError(ValueError):
    """A safe validation error suitable for display in the staff UI."""


@dataclass(frozen=True)
class UserListItem:
    id: UUID
    username: str
    full_name: str
    email: str | None
    role_names: tuple[str, ...]
    status_key: str
    status_label: str
    last_login_display: str | None
    created_display: str


class UserService:
    def __init__(self, db: Session):
        self.db = db

    def list_users(
        self,
        search: str | None = None,
        *,
        now: datetime | None = None,
    ) -> tuple[UserListItem, ...]:
        query = select(User).options(selectinload(User.roles))
        normalized_search = (search or "").strip()
        if normalized_search:
            pattern = f"%{normalized_search}%"
            query = query.where(
                or_(
                    User.username.ilike(pattern),
                    User.full_name.ilike(pattern),
                    User.email.ilike(pattern),
                )
            )

        users = self.db.scalars(query.order_by(User.username)).unique().all()
        timezone_name = self._display_timezone()
        checked_at = now or datetime.now(timezone.utc)
        return tuple(
            self._to_list_item(user, checked_at, timezone_name)
            for user in users
        )

    def list_roles(self) -> tuple[Role, ...]:
        return tuple(
            self.db.scalars(select(Role).order_by(Role.name, Role.code)).all()
        )

    def username_exists(self, username: str) -> bool:
        return self.db.scalar(
            select(User.id).where(func.lower(User.username) == username.lower())
        ) is not None

    def email_exists(self, email: str) -> bool:
        return self.db.scalar(
            select(User.id).where(func.lower(User.email) == email.lower())
        ) is not None

    def create_user(
        self,
        *,
        username: str,
        full_name: str,
        email: str | None,
        role_ids: list[str],
        temporary_password: str,
        confirm_password: str,
        is_active: bool,
    ) -> User:
        clean_username = username.strip()
        clean_full_name = full_name.strip()
        clean_email = (email or "").strip() or None

        if not clean_username:
            raise UserValidationError("Username is required.")
        if len(clean_username) > 80:
            raise UserValidationError("Username must contain at most 80 characters.")
        if not clean_full_name:
            raise UserValidationError("Full name is required.")
        if len(clean_full_name) > 150:
            raise UserValidationError("Full name must contain at most 150 characters.")
        if clean_email is not None:
            if len(clean_email) > 255:
                raise UserValidationError("Email must contain at most 255 characters.")
            if not _EMAIL_PATTERN.fullmatch(clean_email):
                raise UserValidationError("Enter a valid email address.")
        if len(temporary_password) < 12:
            raise UserValidationError(
                "Temporary password must contain at least 12 characters."
            )
        if temporary_password != confirm_password:
            raise UserValidationError("Temporary passwords do not match.")
        if self.username_exists(clean_username):
            raise UserValidationError("That username is already in use.")
        if clean_email is not None and self.email_exists(clean_email):
            raise UserValidationError("That email address is already in use.")

        parsed_role_ids: set[UUID] = set()
        try:
            parsed_role_ids = {UUID(value) for value in role_ids}
        except (ValueError, TypeError, AttributeError) as error:
            raise UserValidationError("One or more selected roles are invalid.") from error
        if not parsed_role_ids:
            raise UserValidationError("Select at least one role.")

        roles = self.db.scalars(
            select(Role).where(Role.id.in_(parsed_role_ids)).order_by(Role.name)
        ).all()
        if {role.id for role in roles} != parsed_role_ids:
            raise UserValidationError("One or more selected roles are invalid.")

        try:
            with self.db.begin_nested():
                user = User(
                    username=clean_username,
                    full_name=clean_full_name,
                    email=clean_email,
                    password_hash=hash_password(temporary_password),
                    is_active=is_active,
                    must_change_password=True,
                    failed_login_count=0,
                    locked_until=None,
                    roles=list(roles),
                )
                self.db.add(user)
                self.db.flush()
        except IntegrityError as error:
            raise UserValidationError(
                "The username or email address is already in use."
            ) from error
        return user

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

    @staticmethod
    def _to_list_item(
        user: User,
        now: datetime,
        timezone_name: str,
    ) -> UserListItem:
        if not user.is_active:
            status_key, status_label = "disabled", "Inactive"
        elif user.locked_until is not None and _as_utc(user.locked_until) > _as_utc(now):
            status_key, status_label = "warning", "Locked"
        elif user.must_change_password:
            status_key, status_label = "warning", "Password change required"
        else:
            status_key, status_label = "active", "Active"

        return UserListItem(
            id=user.id,
            username=user.username,
            full_name=user.full_name,
            email=user.email,
            role_names=tuple(sorted(role.name for role in user.roles)),
            status_key=status_key,
            status_label=status_label,
            last_login_display=_display_time(user.last_login_at, timezone_name),
            created_display=_display_time(user.created_at, timezone_name) or "Unknown",
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _display_time(value: datetime | None, timezone_name: str) -> str | None:
    if value is None:
        return None
    try:
        display_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        display_timezone = timezone.utc
    return _as_utc(value).astimezone(display_timezone).strftime("%Y-%m-%d %H:%M:%S")
