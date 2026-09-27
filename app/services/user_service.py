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
from app.models.associations import user_roles
from app.models.role import Role
from app.models.site import Site
from app.models.user import User
from app.services.account_service import PasswordChangeError, validate_password_pair
from app.services.session_service import revoke_all_user_sessions


_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ADMINISTRATOR_ROLE_CODE = "administrator"


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


@dataclass(frozen=True)
class UserUpdateResult:
    user: User
    updated_fields: tuple[str, ...]
    activation_action: str | None
    roles_changed: bool
    sessions_revoked: int


@dataclass(frozen=True)
class PasswordResetResult:
    user: User
    sessions_revoked: int


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

    def get_user(self, user_id: UUID) -> User | None:
        return self.db.scalar(
            select(User)
            .options(selectinload(User.roles))
            .where(User.id == user_id)
        )

    def username_exists(self, username: str) -> bool:
        return self.db.scalar(
            select(User.id).where(func.lower(User.username) == username.lower())
        ) is not None

    def email_exists(
        self,
        email: str,
        *,
        exclude_user_id: UUID | None = None,
    ) -> bool:
        query = select(User.id).where(func.lower(User.email) == email.lower())
        if exclude_user_id is not None:
            query = query.where(User.id != exclude_user_id)
        return self.db.scalar(query) is not None

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
        self._validate_temporary_password(temporary_password, confirm_password)
        if self.username_exists(clean_username):
            raise UserValidationError("That username is already in use.")
        if clean_email is not None and self.email_exists(clean_email):
            raise UserValidationError("That email address is already in use.")

        roles = self._resolve_roles(role_ids)

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

    def update_user(
        self,
        target: User,
        *,
        actor_user_id: UUID,
        full_name: str,
        email: str | None,
        role_ids: list[str],
        is_active: bool,
    ) -> UserUpdateResult:
        clean_full_name = full_name.strip()
        clean_email = (email or "").strip() or None
        self._validate_edit_profile(clean_full_name, clean_email, target.id)
        roles = self._resolve_roles(role_ids)

        previous_role_ids = {role.id for role in target.roles}
        next_role_ids = {role.id for role in roles}
        roles_changed = previous_role_ids != next_role_ids
        activation_changed = target.is_active != is_active

        if target.id == actor_user_id and not is_active:
            raise UserValidationError("You cannot disable your own account.")

        administrator_role = self.db.scalar(
            select(Role)
            .where(Role.code == _ADMINISTRATOR_ROLE_CODE)
            .with_for_update()
        )
        if administrator_role is not None:
            was_active_administrator = (
                target.is_active and administrator_role.id in previous_role_ids
            )
            will_be_active_administrator = (
                is_active and administrator_role.id in next_role_ids
            )
            if was_active_administrator and not will_be_active_administrator:
                active_administrator_count = self.db.scalar(
                    select(func.count(User.id))
                    .select_from(User)
                    .join(user_roles, user_roles.c.user_id == User.id)
                    .where(
                        user_roles.c.role_id == administrator_role.id,
                        User.is_active.is_(True),
                    )
                ) or 0
                if active_administrator_count <= 1:
                    raise UserValidationError(
                        "The last active administrator cannot be disabled or "
                        "have the administrator role removed."
                    )

        updated_fields: list[str] = []
        if target.full_name != clean_full_name:
            updated_fields.append("full_name")
        if target.email != clean_email:
            updated_fields.append("email")
        if roles_changed:
            updated_fields.append("roles")

        try:
            with self.db.begin_nested():
                target.full_name = clean_full_name
                target.email = clean_email
                target.roles = list(roles)
                target.is_active = is_active
                self.db.flush()
        except IntegrityError as error:
            raise UserValidationError(
                "That email address is already in use."
            ) from error

        sessions_revoked = 0
        if roles_changed or (activation_changed and not is_active):
            sessions_revoked = revoke_all_user_sessions(self.db, target.id)

        activation_action = None
        if activation_changed:
            activation_action = "user.enable" if is_active else "user.disable"

        return UserUpdateResult(
            user=target,
            updated_fields=tuple(updated_fields),
            activation_action=activation_action,
            roles_changed=roles_changed,
            sessions_revoked=sessions_revoked,
        )

    def reset_password(
        self,
        target: User,
        *,
        actor_user_id: UUID,
        temporary_password: str,
        confirm_password: str,
    ) -> PasswordResetResult:
        if target.id == actor_user_id:
            raise UserValidationError(
                "Use Change Password to update your own password."
            )
        self._validate_temporary_password(temporary_password, confirm_password)
        target.password_hash = hash_password(temporary_password)
        target.must_change_password = True
        self.db.flush()
        sessions_revoked = revoke_all_user_sessions(self.db, target.id)
        return PasswordResetResult(
            user=target,
            sessions_revoked=sessions_revoked,
        )

    def unlock_user(self, target: User) -> None:
        target.failed_login_count = 0
        target.locked_until = None
        self.db.flush()

    @staticmethod
    def _validate_temporary_password(
        password: str,
        confirmation: str,
    ) -> None:
        try:
            validate_password_pair(
                password,
                confirmation,
                singular_label="Temporary password",
                plural_label="Temporary passwords",
            )
        except PasswordChangeError as error:
            raise UserValidationError(str(error)) from error

    def _validate_edit_profile(
        self,
        full_name: str,
        email: str | None,
        target_user_id: UUID,
    ) -> None:
        if not full_name:
            raise UserValidationError("Full name is required.")
        if len(full_name) > 150:
            raise UserValidationError("Full name must contain at most 150 characters.")
        if email is not None:
            if len(email) > 255:
                raise UserValidationError("Email must contain at most 255 characters.")
            if not _EMAIL_PATTERN.fullmatch(email):
                raise UserValidationError("Enter a valid email address.")
            if self.email_exists(email, exclude_user_id=target_user_id):
                raise UserValidationError("That email address is already in use.")

    def _resolve_roles(self, role_ids: list[str]) -> list[Role]:
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
        return list(roles)

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
