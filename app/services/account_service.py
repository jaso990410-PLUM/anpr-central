from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.models.user import User


class PasswordChangeError(ValueError):
    """A safe validation error that can be shown in the account form."""


def change_password(
    db: Session,
    user: User,
    *,
    current_password: str,
    new_password: str,
    confirm_password: str,
) -> None:
    """Validate and stage a user's own password change."""
    if not verify_password(current_password, user.password_hash):
        raise PasswordChangeError("Current password is incorrect.")

    if len(new_password) < 12:
        raise PasswordChangeError(
            "New password must contain at least 12 characters."
        )

    if new_password != confirm_password:
        raise PasswordChangeError("New passwords do not match.")

    if verify_password(new_password, user.password_hash):
        raise PasswordChangeError(
            "New password must be different from the current password."
        )

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.failed_login_count = 0
    user.locked_until = None
    db.flush()
