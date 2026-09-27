from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.db.dependencies import get_db
from app.models.role import Role
from app.models.user import User
from app.services.session_service import find_valid_session, revoke_session


class PageAuthenticationRequired(Exception):
    """Raised when a server-rendered page needs a login redirect."""


class PasswordChangeRequired(Exception):
    """Raised when a page user must update their password first."""


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User | None:
    """Return the active user for a valid server-side browser session."""
    raw_token = request.cookies.get(settings.session_cookie_name)
    if not raw_token:
        return None

    user_session = find_valid_session(db, raw_token)
    if user_session is None:
        return None

    user = db.scalar(
        select(User)
        .options(selectinload(User.roles).selectinload(Role.permissions))
        .where(User.id == user_session.user_id)
    )
    if user is None or not user.is_active:
        revoke_session(db, raw_token)
        db.commit()
        return None

    # Persist the idle-timeout activity update before route handling begins.
    db.commit()
    return user


def require_authenticated_user(
    user: User | None = Depends(get_current_user),
) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password change required",
        )
    return user


def require_authenticated_page_user(
    user: User | None = Depends(get_current_user),
) -> User:
    if user is None:
        raise PageAuthenticationRequired
    return user


def require_authenticated_user_page(
    user: User = Depends(require_authenticated_page_user),
) -> User:
    if user.must_change_password:
        raise PasswordChangeRequired
    return user


def user_has_permission(user: User, permission_code: str) -> bool:
    return any(
        permission.code == permission_code
        for role in user.roles
        for permission in role.permissions
    )


def require_permission(permission_code: str) -> Callable[..., User]:
    """Build a FastAPI dependency enforcing a backend permission check."""
    def permission_dependency(
        user: User = Depends(require_authenticated_user),
    ) -> User:
        if not user_has_permission(user, permission_code):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permission denied",
            )
        return user

    return permission_dependency
