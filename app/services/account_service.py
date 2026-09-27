from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.models.user import User


class PasswordChangeError(ValueError):
    """A safe validation error that can be shown in the account form."""


def validate_password_pair(
    password: str,
    confirmation: str,
    *,
    singular_label: str = "New password",
    plural_label: str = "New passwords",
) -> None:
    """Apply the shared password length and confirmation policy."""
    if len(password) < 12:
        raise PasswordChangeError(
            f"{singular_label} must contain at least 12 characters."
        )
    if password != confirmation:
        raise PasswordChangeError(f"{plural_label} do not match.")


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

    validate_password_pair(new_password, confirm_password)

    if verify_password(new_password, user.password_hash):
        raise PasswordChangeError(
            "New password must be different from the current password."
        )

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.failed_login_count = 0
    user.locked_until = None
    db.flush()
