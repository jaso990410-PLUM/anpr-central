from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.csrf import generate_csrf_token, verify_csrf_token
from app.core.security import hash_password, hash_session_token, verify_password
from app.dependencies.auth import require_permission
from app.models.permission import Permission
from app.models.role import Role
from app.models.user import User
from app.models.user_session import UserSession
from app.services.audit_service import create_audit_log
from app.services.auth_service import (
    INVALID_CREDENTIALS_MESSAGE,
    AuthenticationError,
    authenticate_user,
)
from app.services.session_service import (
    create_session,
    find_valid_session,
    revoke_session,
)


NOW = datetime(2026, 9, 27, 4, 0, tzinfo=timezone.utc)


def make_user(
    db: Session,
    *,
    username: str = "alice",
    password: str = "correct horse battery staple",
    is_active: bool = True,
) -> User:
    user = User(
        username=username,
        full_name="Alice Example",
        password_hash=hash_password(password),
        is_active=is_active,
        must_change_password=False,
        failed_login_count=0,
    )
    db.add(user)
    db.flush()
    return user


def test_valid_argon2_password() -> None:
    password_hash = hash_password("a strong password")

    assert password_hash.startswith("$argon2")
    assert verify_password("a strong password", password_hash)


def test_invalid_password() -> None:
    password_hash = hash_password("a strong password")

    assert not verify_password("not the password", password_hash)
    assert not verify_password("anything", "not-an-argon2-hash")


def test_inactive_user_is_rejected_with_generic_error(db: Session) -> None:
    make_user(db, is_active=False)

    with pytest.raises(AuthenticationError) as error:
        authenticate_user(
            db,
            "alice",
            "correct horse battery staple",
            now=NOW,
        )

    assert str(error.value) == INVALID_CREDENTIALS_MESSAGE


def test_login_failure_counter_increments(db: Session) -> None:
    user = make_user(db)

    with pytest.raises(AuthenticationError) as error:
        authenticate_user(db, "alice", "wrong password", now=NOW)

    assert str(error.value) == INVALID_CREDENTIALS_MESSAGE
    assert user.failed_login_count == 1
    assert user.locked_until is None


def test_account_locks_at_configured_failure_threshold(db: Session) -> None:
    user = make_user(db)

    for attempt in range(settings.login_max_failures):
        with pytest.raises(AuthenticationError):
            authenticate_user(
                db,
                "alice",
                f"wrong password {attempt}",
                now=NOW,
            )

    assert user.failed_login_count == settings.login_max_failures
    assert user.locked_until == NOW + timedelta(
        minutes=settings.login_lock_minutes
    )

    with pytest.raises(AuthenticationError) as error:
        authenticate_user(
            db,
            "alice",
            "correct horse battery staple",
            now=NOW + timedelta(minutes=1),
        )

    assert str(error.value) == INVALID_CREDENTIALS_MESSAGE


def test_successful_authentication_resets_failures(db: Session) -> None:
    user = make_user(db)
    user.failed_login_count = 2
    user.locked_until = NOW - timedelta(minutes=1)

    authenticated = authenticate_user(
        db,
        "alice",
        "correct horse battery staple",
        now=NOW,
    )

    assert authenticated is user
    assert user.failed_login_count == 0
    assert user.locked_until is None
    assert user.last_login_at == NOW


def test_valid_session_stores_only_sha256_hash(db: Session) -> None:
    user = make_user(db)
    raw_token, created = create_session(db, user, now=NOW)

    stored = db.scalar(select(UserSession).where(UserSession.id == created.id))
    found = find_valid_session(
        db,
        raw_token,
        now=NOW + timedelta(minutes=1),
        touch=False,
    )

    assert stored is not None
    assert stored.token_hash == hash_session_token(raw_token)
    assert raw_token != stored.token_hash
    assert len(stored.token_hash) == 64
    assert found is created


def test_expired_session_is_rejected(db: Session) -> None:
    user = make_user(db)
    issued_at = NOW - timedelta(minutes=settings.session_lifetime_minutes + 1)
    raw_token, _ = create_session(db, user, now=issued_at)

    assert find_valid_session(db, raw_token, now=NOW) is None


def test_idle_session_is_rejected(db: Session) -> None:
    user = make_user(db)
    issued_at = NOW - timedelta(minutes=settings.session_idle_minutes + 1)
    raw_token, _ = create_session(db, user, now=issued_at)

    assert find_valid_session(db, raw_token, now=NOW) is None


def test_revoked_session_is_rejected(db: Session) -> None:
    user = make_user(db)
    raw_token, _ = create_session(db, user, now=NOW)

    assert revoke_session(db, raw_token, now=NOW + timedelta(minutes=1))
    assert find_valid_session(
        db,
        raw_token,
        now=NOW + timedelta(minutes=2),
    ) is None


def test_permission_allowed() -> None:
    permission = Permission(code="cameras.view")
    role = Role(code="operator", name="Operator", permissions=[permission])
    user = User(
        username="operator",
        full_name="Operator",
        password_hash="unused",
        roles=[role],
    )

    checker = require_permission("cameras.view")

    assert checker(user) is user


def test_permission_denied() -> None:
    role = Role(code="viewer", name="Viewer", permissions=[])
    user = User(
        username="viewer",
        full_name="Viewer",
        password_hash="unused",
        roles=[role],
    )

    checker = require_permission("users.manage")

    with pytest.raises(HTTPException) as error:
        checker(user)

    assert error.value.status_code == 403


def test_csrf_token_is_bound_to_session() -> None:
    csrf_token = generate_csrf_token("browser-session-token")

    assert verify_csrf_token(csrf_token, "browser-session-token")
    assert not verify_csrf_token(csrf_token, "different-session-token")


def test_audit_details_remove_credentials(db: Session) -> None:
    audit_log = create_audit_log(
        db,
        action="login.failed",
        details={
            "username": "alice",
            "password": "must-not-be-stored",
            "nested": {"session_token": "must-not-be-stored"},
        },
    )

    assert audit_log.details == {"username": "alice", "nested": {}}
