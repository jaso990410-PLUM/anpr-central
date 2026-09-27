import re
import uuid
from collections.abc import Generator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.security import (
    hash_password,
    hash_session_token,
    verify_password,
)
from app.db.dependencies import get_db
from app.main import app
from app.models.audit_log import AuditLog
from app.models.camera import Camera
from app.models.permission import Permission
from app.models.role import Role
from app.models.site import Site
from app.models.user import User
from app.models.user_session import UserSession
from app.services.audit_service import create_audit_log
from app.services.camera_service import (
    CREDENTIALS_REVOKED,
    DISABLED,
    UNPROVISIONED,
    CameraService,
)
from app.services.session_service import create_session, find_valid_session
from app.services.user_service import UserService


TEST_PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "new correct horse battery staple"


def create_user(
    db: Session,
    *,
    permissions: tuple[str, ...] = (),
    must_change_password: bool = False,
) -> User:
    role = Role(
        code="administrator",
        name="Administrator",
        is_system=True,
    )
    role.permissions = [
        Permission(code=code, description=f"Test permission: {code}")
        for code in permissions
    ]
    user = User(
        username="admin",
        full_name="System Administrator",
        password_hash=hash_password(TEST_PASSWORD),
        is_active=True,
        must_change_password=must_change_password,
        failed_login_count=0,
        roles=[role],
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def client(db: Session) -> Generator[TestClient, None, None]:
    request_session = sessionmaker(
        bind=db.get_bind(),
        expire_on_commit=False,
    )

    def override_get_db() -> Generator[Session, None, None]:
        with request_session() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def extract_csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def login(
    client: TestClient,
    *,
    username: str = "admin",
    password: str = TEST_PASSWORD,
    next_destination: str = "/dashboard",
):
    login_page = client.get("/login")
    csrf_token = extract_csrf_token(login_page.text)
    return client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": csrf_token,
            "next": next_destination,
        },
    )


def submit_password_change(
    client: TestClient,
    *,
    current_password: str = TEST_PASSWORD,
    new_password: str = NEW_PASSWORD,
    confirm_password: str = NEW_PASSWORD,
):
    page = client.get("/account/change-password")
    assert page.status_code == 200
    csrf_token = extract_csrf_token(page.text)
    return client.post(
        "/account/change-password",
        data={
            "current_password": current_password,
            "new_password": new_password,
            "confirm_password": confirm_password,
            "csrf_token": csrf_token,
        },
    )


def test_login_page_loads_with_secure_form(client: TestClient) -> None:
    response = client.get("/login")

    assert response.status_code == 200
    assert "ANPR Central" in response.text
    assert 'name="username"' in response.text
    assert 'name="password"' in response.text
    assert 'name="csrf_token"' in response.text
    assert "Vehicle operations" in response.text
    assert "System online" in response.text
    assert "VAB 1234" in response.text
    assert "Recognition preview" in response.text
    assert settings.csrf_cookie_name in response.cookies


@pytest.mark.parametrize("csrf_token", [None, "invalid"])
def test_login_rejects_missing_or_invalid_csrf(
    client: TestClient,
    db: Session,
    csrf_token: str | None,
) -> None:
    create_user(db)
    client.get("/login")
    data = {
        "username": "admin",
        "password": TEST_PASSWORD,
    }
    if csrf_token is not None:
        data["csrf_token"] = csrf_token

    response = client.post("/login", data=data)

    assert response.status_code == 400
    assert "form expired" in response.text


def test_root_and_protected_page_redirect_when_logged_out(
    client: TestClient,
) -> None:
    root_response = client.get("/")
    dashboard_response = client.get("/dashboard")

    assert root_response.status_code == 303
    assert root_response.headers["location"] == "/login"
    assert dashboard_response.status_code == 303
    assert dashboard_response.headers["location"] == (
        "/login?next=%2Fdashboard"
    )


def test_wrong_password_is_rejected_generically(
    client: TestClient,
    db: Session,
) -> None:
    user = create_user(db)

    response = login(client, password="wrong password")

    assert response.status_code == 401
    assert "Invalid username or password" in response.text
    db.refresh(user)
    assert user.failed_login_count == 1

    audit = db.scalar(
        select(AuditLog).where(AuditLog.action == "login.failed")
    )
    assert audit is not None
    assert audit.details == {"username": "admin"}


def test_correct_login_creates_hashed_session_and_cookie(
    client: TestClient,
    db: Session,
) -> None:
    create_user(db, permissions=("users.view",))

    response = login(client)

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    raw_token = client.cookies.get(settings.session_cookie_name)
    assert raw_token is not None

    stored_session = db.scalar(select(UserSession))
    assert stored_session is not None
    assert stored_session.token_hash == hash_session_token(raw_token)
    assert stored_session.token_hash != raw_token

    cookie_header = next(
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{settings.session_cookie_name}=")
    )
    assert "HttpOnly" in cookie_header
    assert "SameSite=lax" in cookie_header
    assert "Path=/" in cookie_header
    assert ("Secure" in cookie_header) is settings.cookie_secure

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "Operations overview" in dashboard.text
    assert "Camera status" in dashboard.text
    assert "System status" in dashboard.text
    assert 'data-nav-key="users"' in dashboard.text

    root = client.get("/")
    assert root.status_code == 303
    assert root.headers["location"] == "/dashboard"


def test_dashboard_renders_real_database_values(
    client: TestClient,
    db: Session,
) -> None:
    user = create_user(db)
    site = Site(
        code="MAIN",
        name="Main Site",
        timezone="Asia/Kuala_Lumpur",
        is_active=True,
    )
    db.add(site)
    db.flush()
    db.add(
        Camera(
            site_id=site.id,
            code="MAIN-ENTRY-01",
            name="Main Entry",
            direction="entry",
            ip_address="192.0.2.10",
            is_enabled=True,
        )
    )
    db.commit()

    assert login(client).status_code == 303
    dashboard = client.get("/dashboard")

    assert dashboard.status_code == 200
    assert "Main Site" in dashboard.text
    assert "MAIN" in dashboard.text
    assert "Asia/Kuala_Lumpur" in dashboard.text
    assert "MAIN-ENTRY-01" in dashboard.text
    assert "192.0.2.10" in dashboard.text
    assert user.username in dashboard.text


@pytest.mark.parametrize(
    ("permissions", "users_visible"),
    [
        (("users.view",), True),
        (("cameras.view",), False),
    ],
)
def test_dashboard_navigation_respects_users_view_permission(
    client: TestClient,
    db: Session,
    permissions: tuple[str, ...],
    users_visible: bool,
) -> None:
    create_user(db, permissions=permissions)
    assert login(client).status_code == 303

    dashboard = client.get("/dashboard")

    assert dashboard.status_code == 200
    assert ('data-nav-key="users"' in dashboard.text) is users_visible


def test_inactive_user_session_cannot_access_dashboard(
    client: TestClient,
    db: Session,
) -> None:
    user = create_user(db)
    assert login(client).status_code == 303
    user.is_active = False
    db.commit()

    response = client.get("/dashboard")

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fdashboard"
    db.expire_all()
    stored_session = db.scalar(select(UserSession))
    assert stored_session is not None
    assert stored_session.revoked_at is not None


def test_logout_revokes_session_and_old_cookie_stops_working(
    client: TestClient,
    db: Session,
) -> None:
    create_user(db)
    login_response = login(client)
    assert login_response.status_code == 303
    raw_token = client.cookies.get(settings.session_cookie_name)
    assert raw_token is not None

    dashboard = client.get("/dashboard")
    logout_csrf_token = extract_csrf_token(dashboard.text)
    response = client.post(
        "/logout",
        data={"csrf_token": logout_csrf_token},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert settings.session_cookie_name not in client.cookies

    db.expire_all()
    stored_session = db.scalar(
        select(UserSession).where(
            UserSession.token_hash == hash_session_token(raw_token)
        )
    )
    assert stored_session is not None
    assert stored_session.revoked_at is not None

    client.cookies.set(settings.session_cookie_name, raw_token)
    rejected = client.get("/dashboard")
    assert rejected.status_code == 303
    assert rejected.headers["location"] == "/login?next=%2Fdashboard"


@pytest.mark.parametrize("form_data", [{}, {"csrf_token": "invalid"}])
def test_logout_rejects_missing_or_invalid_csrf_token(
    client: TestClient,
    db: Session,
    form_data: dict[str, str],
) -> None:
    create_user(db)
    login(client)

    response = client.post(
        "/logout",
        data=form_data,
    )

    assert response.status_code == 403
    stored_session = db.scalar(select(UserSession))
    assert stored_session is not None
    assert stored_session.revoked_at is None


@pytest.mark.parametrize(
    "unsafe_destination",
    [
        "https://evil.example/steal",
        "//evil.example/steal",
        "/%2F%2Fevil.example/steal",
        "/\\evil.example/steal",
    ],
)
def test_login_rejects_unsafe_next_redirects(
    client: TestClient,
    db: Session,
    unsafe_destination: str,
) -> None:
    create_user(db)

    response = login(client, next_destination=unsafe_destination)

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


def test_change_password_page_requires_authentication(
    client: TestClient,
) -> None:
    response = client.get("/account/change-password")

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/login?next=%2Faccount%2Fchange-password"
    )


@pytest.mark.parametrize(
    ("current_password", "new_password", "confirm_password", "message"),
    [
        (
            "incorrect current password",
            NEW_PASSWORD,
            NEW_PASSWORD,
            "Current password is incorrect.",
        ),
        (
            TEST_PASSWORD,
            "too short",
            "too short",
            "New password must contain at least 12 characters.",
        ),
        (
            TEST_PASSWORD,
            NEW_PASSWORD,
            "different confirmation password",
            "New passwords do not match.",
        ),
        (
            TEST_PASSWORD,
            TEST_PASSWORD,
            TEST_PASSWORD,
            "New password must be different from the current password.",
        ),
    ],
)
def test_change_password_validation(
    client: TestClient,
    db: Session,
    current_password: str,
    new_password: str,
    confirm_password: str,
    message: str,
) -> None:
    user = create_user(db)
    assert login(client).status_code == 303

    response = submit_password_change(
        client,
        current_password=current_password,
        new_password=new_password,
        confirm_password=confirm_password,
    )

    assert response.status_code == 400
    assert message in response.text
    db.refresh(user)
    assert verify_password(TEST_PASSWORD, user.password_hash)


@pytest.mark.parametrize("csrf_token", [None, "invalid"])
def test_change_password_rejects_missing_or_invalid_csrf(
    client: TestClient,
    db: Session,
    csrf_token: str | None,
) -> None:
    create_user(db)
    assert login(client).status_code == 303
    data = {
        "current_password": TEST_PASSWORD,
        "new_password": NEW_PASSWORD,
        "confirm_password": NEW_PASSWORD,
    }
    if csrf_token is not None:
        data["csrf_token"] = csrf_token

    response = client.post("/account/change-password", data=data)

    assert response.status_code == 403


def test_successful_password_change_replaces_password_and_audits(
    client: TestClient,
    db: Session,
) -> None:
    user = create_user(db)
    assert login(client).status_code == 303

    response = submit_password_change(client)

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    db.refresh(user)
    assert user.password_hash.startswith("$argon2")
    assert not verify_password(TEST_PASSWORD, user.password_hash)
    assert verify_password(NEW_PASSWORD, user.password_hash)
    assert not user.must_change_password

    audit = db.scalar(
        select(AuditLog).where(
            AuditLog.action == "account.password_change"
        )
    )
    assert audit is not None
    assert audit.actor_user_id == user.id
    assert audit.details == {"sessions_revoked": 1}
    assert "password" not in str(audit.details).lower()

    dashboard = client.get("/dashboard")
    logout_csrf_token = extract_csrf_token(dashboard.text)
    assert client.post(
        "/logout",
        data={"csrf_token": logout_csrf_token},
    ).status_code == 303

    assert login(client, password=TEST_PASSWORD).status_code == 401
    assert login(client, password=NEW_PASSWORD).status_code == 303


def test_password_change_revokes_other_sessions_and_rotates_current(
    client: TestClient,
    db: Session,
) -> None:
    user = create_user(db)
    other_raw_token, _ = create_session(db, user)
    db.commit()
    assert login(client).status_code == 303
    old_current_token = client.cookies.get(settings.session_cookie_name)
    assert old_current_token is not None

    assert submit_password_change(client).status_code == 303
    new_current_token = client.cookies.get(settings.session_cookie_name)
    assert new_current_token is not None
    assert new_current_token != old_current_token

    assert find_valid_session(db, other_raw_token, touch=False) is None
    assert find_valid_session(db, old_current_token, touch=False) is None
    assert find_valid_session(db, new_current_token, touch=False) is not None


def test_forced_password_change_blocks_dashboard_but_allows_logout(
    client: TestClient,
    db: Session,
) -> None:
    create_user(db, must_change_password=True)

    login_response = login(client)
    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/account/change-password"

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 303
    assert dashboard.headers["location"] == "/account/change-password"

    account_page = client.get("/account/change-password")
    assert account_page.status_code == 200
    assert "Password update required" in account_page.text
    assert 'data-nav-key="dashboard"' not in account_page.text

    csrf_token = extract_csrf_token(account_page.text)
    logout_response = client.post(
        "/logout",
        data={"csrf_token": csrf_token},
    )
    assert logout_response.status_code == 303
    assert logout_response.headers["location"] == "/login"


def test_forced_password_change_clears_flag_and_unlocks_dashboard(
    client: TestClient,
    db: Session,
) -> None:
    user = create_user(db, must_change_password=True)
    assert login(client).headers["location"] == "/account/change-password"

    response = submit_password_change(client)

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    db.refresh(user)
    assert not user.must_change_password
    assert client.get("/dashboard").status_code == 200


def create_role(db: Session, code: str, name: str) -> Role:
    role = Role(code=code, name=name, is_system=True)
    db.add(role)
    db.commit()
    return role


def submit_new_user(
    client: TestClient,
    selected_role_ids: list[str],
    **overrides: object,
):
    page = client.get("/users/new")
    assert page.status_code == 200
    data: dict[str, object] = {
        "username": "jamilah",
        "full_name": "Jamilah Hassan",
        "email": "jamilah@example.test",
        "role_ids": selected_role_ids,
        "temporary_password": "temporary secure passphrase",
        "confirm_password": "temporary secure passphrase",
        "is_active": "true",
        "csrf_token": extract_csrf_token(page.text),
    }
    data.update(overrides)
    return client.post("/users/new", data=data)


def test_user_list_redirects_anonymous_user(client: TestClient) -> None:
    response = client.get("/users")

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fusers"


def test_user_list_requires_users_view_permission(
    client: TestClient,
    db: Session,
) -> None:
    create_user(db, permissions=("cameras.view",))
    assert login(client).status_code == 303

    response = client.get("/users")

    assert response.status_code == 403
    assert response.json() == {"detail": "Permission denied"}


def test_user_list_renders_real_database_users_and_manage_action(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(db, permissions=("users.view", "users.manage"))
    actor.email = "admin@example.test"
    db.commit()
    assert login(client).status_code == 303

    response = client.get("/users")

    assert response.status_code == 200
    assert "System Administrator" in response.text
    assert "admin@example.test" in response.text
    assert "Administrator" in response.text
    assert "Add user" in response.text
    assert 'href="/users/new"' in response.text
    assert f'href="/users/{actor.id}/edit"' in response.text
    assert 'data-nav-key="users"' in response.text


def test_user_list_hides_manage_action_without_users_manage(
    client: TestClient,
    db: Session,
) -> None:
    create_user(db, permissions=("users.view",))
    assert login(client).status_code == 303

    response = client.get("/users")

    assert response.status_code == 200
    assert 'href="/users/new"' not in response.text


@pytest.mark.parametrize(
    ("query", "expected_username"),
    [
        ("CAMERAOPS", "cameraops"),
        ("nur aisyah", "gateagent"),
        ("mailtarget@EXAMPLE.test", "mailagent"),
    ],
)
def test_user_list_searches_username_full_name_and_email_case_insensitively(
    client: TestClient,
    db: Session,
    query: str,
    expected_username: str,
) -> None:
    actor = create_user(db, permissions=("users.view",))
    db.add_all(
        [
            User(
                username="cameraops",
                full_name="Camera Operator",
                email="camera@example.test",
                password_hash=hash_password(TEST_PASSWORD),
                is_active=True,
                must_change_password=False,
                roles=[actor.roles[0]],
            ),
            User(
                username="gateagent",
                full_name="Nur Aisyah Rahman",
                email="gate@example.test",
                password_hash=hash_password(TEST_PASSWORD),
                is_active=True,
                must_change_password=False,
                roles=[actor.roles[0]],
            ),
            User(
                username="mailagent",
                full_name="Email Match",
                email="mailtarget@example.test",
                password_hash=hash_password(TEST_PASSWORD),
                is_active=True,
                must_change_password=False,
                roles=[actor.roles[0]],
            ),
        ]
    )
    db.commit()
    assert login(client).status_code == 303

    response = client.get("/users", params={"q": query})

    assert response.status_code == 200
    assert expected_username in response.text
    for excluded in {"cameraops", "gateagent", "mailagent"} - {expected_username}:
        assert excluded not in response.text


def test_user_list_uses_status_priority(
    db: Session,
) -> None:
    user = create_user(db, permissions=("users.view",))
    user.is_active = False
    user.locked_until = datetime.now(timezone.utc) + timedelta(hours=1)
    user.must_change_password = True
    db.add_all(
        [
            User(
                username="locked-user",
                full_name="Locked User",
                password_hash=hash_password(TEST_PASSWORD),
                is_active=True,
                must_change_password=True,
                locked_until=datetime.now(timezone.utc) + timedelta(hours=1),
                roles=[user.roles[0]],
            ),
            User(
                username="password-user",
                full_name="Password User",
                password_hash=hash_password(TEST_PASSWORD),
                is_active=True,
                must_change_password=True,
                roles=[user.roles[0]],
            ),
            User(
                username="active-user",
                full_name="Active User",
                password_hash=hash_password(TEST_PASSWORD),
                is_active=True,
                must_change_password=False,
                roles=[user.roles[0]],
            ),
        ]
    )
    db.commit()

    listed = UserService(db).list_users()

    statuses = {item.username: item.status_label for item in listed}
    assert statuses == {
        "active-user": "Active",
        "admin": "Inactive",
        "locked-user": "Locked",
        "password-user": "Password change required",
    }


def test_new_user_page_requires_users_manage_permission(
    client: TestClient,
    db: Session,
) -> None:
    create_user(db, permissions=("users.view",))
    assert login(client).status_code == 303

    response = client.get("/users/new")

    assert response.status_code == 403

    csrf_token = extract_csrf_token(client.get("/dashboard").text)
    post_response = client.post(
        "/users/new",
        data={
            "username": "unauthorized",
            "full_name": "Unauthorized User",
            "temporary_password": "temporary secure passphrase",
            "confirm_password": "temporary secure passphrase",
            "csrf_token": csrf_token,
        },
    )
    assert post_response.status_code == 403


def test_new_user_page_lists_database_roles(
    client: TestClient,
    db: Session,
) -> None:
    create_user(db, permissions=("users.manage",))
    role = create_role(db, "security", "Security Team")
    assert login(client).status_code == 303

    response = client.get("/users/new")

    assert response.status_code == 200
    assert "Security Team" in response.text
    assert "security" in response.text
    assert str(role.id) in response.text


def test_create_user_stores_roles_argon2_state_and_safe_audit(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(db, permissions=("users.manage",))
    operator_role = create_role(db, "operator", "Operator")
    assert login(client).status_code == 303

    response = submit_new_user(
        client,
        [str(actor.roles[0].id), str(operator_role.id)],
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/users"
    created = db.scalar(select(User).where(User.username == "jamilah"))
    assert created is not None
    assert created.full_name == "Jamilah Hassan"
    assert created.email == "jamilah@example.test"
    assert created.is_active
    assert created.must_change_password
    assert created.failed_login_count == 0
    assert created.locked_until is None
    assert created.password_hash.startswith("$argon2")
    assert verify_password("temporary secure passphrase", created.password_hash)
    assert {role.code for role in created.roles} == {"administrator", "operator"}

    audit = db.scalar(select(AuditLog).where(AuditLog.action == "user.create"))
    assert audit is not None
    assert audit.actor_user_id == actor.id
    assert audit.entity_id == str(created.id)
    assert audit.ip_address == "testclient"
    assert audit.details == {
        "created_user_id": str(created.id),
        "username": "jamilah",
        "role_codes": ["administrator", "operator"],
        "is_active": True,
    }
    audit_text = str(audit.details)
    assert "temporary secure passphrase" not in audit_text
    assert created.password_hash not in audit_text


def test_create_user_stores_inactive_state(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(db, permissions=("users.manage",))
    assert login(client).status_code == 303

    response = submit_new_user(
        client,
        [str(actor.roles[0].id)],
        is_active="",
    )

    assert response.status_code == 303
    created = db.scalar(select(User).where(User.username == "jamilah"))
    assert created is not None
    assert not created.is_active


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"username": "   "}, "Username is required."),
        (
            {"temporary_password": "short pass", "confirm_password": "short pass"},
            "Temporary password must contain at least 12 characters.",
        ),
        (
            {"confirm_password": "another secure passphrase"},
            "Temporary passwords do not match.",
        ),
        ({"role_ids": ["not-a-role-id"]}, "One or more selected roles are invalid."),
        ({"email": "not-an-email"}, "Enter a valid email address."),
    ],
)
def test_create_user_rejects_invalid_form_data(
    client: TestClient,
    db: Session,
    overrides: dict[str, object],
    message: str,
) -> None:
    actor = create_user(db, permissions=("users.manage",))
    assert login(client).status_code == 303

    response = submit_new_user(client, [str(actor.roles[0].id)], **overrides)

    assert response.status_code == 400
    assert message in response.text
    assert "temporary secure passphrase" not in response.text
    assert db.scalar(select(User).where(User.username == "jamilah")) is None


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("username", "admin", "That username is already in use."),
        ("email", "ADMIN@EXAMPLE.TEST", "That email address is already in use."),
    ],
)
def test_create_user_rejects_duplicate_username_or_email(
    client: TestClient,
    db: Session,
    field: str,
    value: str,
    message: str,
) -> None:
    actor = create_user(db, permissions=("users.manage",))
    actor.email = "admin@example.test"
    db.commit()
    assert login(client).status_code == 303

    response = submit_new_user(
        client,
        [str(actor.roles[0].id)],
        **{field: value},
    )

    assert response.status_code == 400
    assert message in response.text


@pytest.mark.parametrize("csrf_token", [None, "invalid"])
def test_create_user_rejects_missing_or_invalid_csrf(
    client: TestClient,
    db: Session,
    csrf_token: str | None,
) -> None:
    actor = create_user(db, permissions=("users.manage",))
    assert login(client).status_code == 303
    data = {
        "username": "jamilah",
        "full_name": "Jamilah Hassan",
        "email": "jamilah@example.test",
        "role_ids": str(actor.roles[0].id),
        "temporary_password": "temporary secure passphrase",
        "confirm_password": "temporary secure passphrase",
        "is_active": "true",
    }
    if csrf_token is not None:
        data["csrf_token"] = csrf_token

    response = client.post("/users/new", data=data)

    assert response.status_code == 403
    assert db.scalar(select(User).where(User.username == "jamilah")) is None


def test_created_user_is_forced_to_change_password_before_dashboard(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(db, permissions=("users.manage",))
    role = create_role(db, "operator", "Operator")
    assert login(client).status_code == 303
    assert submit_new_user(client, [str(role.id)]).status_code == 303

    dashboard = client.get("/dashboard")
    logout_token = extract_csrf_token(dashboard.text)
    assert client.post("/logout", data={"csrf_token": logout_token}).status_code == 303

    response = login(
        client,
        username="jamilah",
        password="temporary secure passphrase",
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account/change-password"
    blocked_dashboard = client.get("/dashboard")
    assert blocked_dashboard.status_code == 303
    assert blocked_dashboard.headers["location"] == "/account/change-password"


@pytest.mark.parametrize("path", ["/register", "/signup"])
def test_self_registration_routes_do_not_exist(
    client: TestClient,
    path: str,
) -> None:
    response = client.get(path)

    assert response.status_code == 404

    login_page = client.get("/login")
    assert 'href="/register"' not in login_page.text
    assert 'href="/signup"' not in login_page.text


def create_staff_user(
    db: Session,
    role: Role,
    *,
    username: str = "operator-one",
    email: str | None = "operator@example.test",
    is_active: bool = True,
) -> User:
    user = User(
        username=username,
        full_name="Original Operator",
        email=email,
        password_hash=hash_password("operator secure passphrase"),
        is_active=is_active,
        must_change_password=False,
        failed_login_count=0,
        roles=[role],
    )
    db.add(user)
    db.commit()
    return user


def submit_user_edit(
    client: TestClient,
    target: User,
    **overrides: object,
):
    page = client.get(f"/users/{target.id}/edit")
    assert page.status_code == 200
    data: dict[str, object] = {
        "full_name": target.full_name,
        "email": target.email or "",
        "role_ids": [str(role.id) for role in target.roles],
        "is_active": "true" if target.is_active else "",
        "csrf_token": extract_csrf_token(page.text),
    }
    data.update(overrides)
    return client.post(f"/users/{target.id}/edit", data=data)


def test_edit_user_requires_users_manage_permission(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(db, permissions=("users.view",))
    assert login(client).status_code == 303

    get_response = client.get(f"/users/{actor.id}/edit")
    csrf_token = extract_csrf_token(client.get("/dashboard").text)
    post_response = client.post(
        f"/users/{actor.id}/edit",
        data={
            "full_name": actor.full_name,
            "role_ids": str(actor.roles[0].id),
            "is_active": "true",
            "csrf_token": csrf_token,
        },
    )

    assert get_response.status_code == 403
    assert post_response.status_code == 403


def test_edit_user_page_uses_database_roles_and_read_only_identity(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(db, permissions=("users.manage",))
    target_role = create_role(db, "operator", "Gate Operator")
    target = create_staff_user(db, target_role)
    assert login(client).status_code == 303

    response = client.get(f"/users/{target.id}/edit")

    assert response.status_code == 200
    assert "Gate Operator" in response.text
    assert str(target_role.id) in response.text
    assert f'value="{target.username}" readonly' in response.text
    assert 'name="username"' not in response.text
    assert 'name="temporary_password" type="password"' in response.text
    assert 'name="password_hash"' not in response.text
    assert "$argon2" not in response.text


def test_edit_user_updates_profile_roles_and_audits_safely(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(db, permissions=("users.manage",))
    operator_role = create_role(db, "operator", "Operator")
    security_role = create_role(db, "security", "Security")
    target = create_staff_user(db, operator_role)
    original_username = target.username
    original_password_hash = target.password_hash
    assert login(client).status_code == 303

    response = submit_user_edit(
        client,
        target,
        full_name="Updated Operator",
        email="updated@example.test",
        role_ids=[str(operator_role.id), str(security_role.id)],
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/users"
    db.expire_all()
    updated = db.get(User, target.id)
    assert updated is not None
    assert updated.username == original_username
    assert updated.password_hash == original_password_hash
    assert updated.full_name == "Updated Operator"
    assert updated.email == "updated@example.test"
    assert {role.code for role in updated.roles} == {"operator", "security"}

    audit = db.scalar(select(AuditLog).where(AuditLog.action == "user.update"))
    assert audit is not None
    assert audit.actor_user_id == actor.id
    assert audit.entity_id == str(target.id)
    assert audit.details["updated_fields"] == ["full_name", "email", "roles"]
    audit_text = str(audit.details).lower()
    assert "password" not in audit_text
    assert "token" not in audit_text
    assert "csrf" not in audit_text
    assert original_password_hash not in audit_text


def test_edit_user_rejects_duplicate_email(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(db, permissions=("users.manage",))
    actor.email = "admin@example.test"
    role = create_role(db, "operator", "Operator")
    target = create_staff_user(db, role)
    db.commit()
    assert login(client).status_code == 303

    response = submit_user_edit(
        client,
        target,
        email="ADMIN@EXAMPLE.TEST",
    )

    assert response.status_code == 400
    assert "That email address is already in use." in response.text
    db.refresh(target)
    assert target.email == "operator@example.test"


def test_edit_user_rejects_invalid_role_id(
    client: TestClient,
    db: Session,
) -> None:
    create_user(db, permissions=("users.manage",))
    role = create_role(db, "operator", "Operator")
    target = create_staff_user(db, role)
    assert login(client).status_code == 303

    response = submit_user_edit(
        client,
        target,
        role_ids=["not-a-role-id"],
    )

    assert response.status_code == 400
    assert "One or more selected roles are invalid." in response.text
    db.refresh(target)
    assert {assigned.code for assigned in target.roles} == {"operator"}


def test_disabling_and_reenabling_user_revokes_sessions_and_audits(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(db, permissions=("users.manage",))
    role = create_role(db, "operator", "Operator")
    target = create_staff_user(db, role)
    target_raw_token, _ = create_session(db, target)
    db.commit()
    assert login(client).status_code == 303

    disable_response = submit_user_edit(client, target, is_active="")

    assert disable_response.status_code == 303
    db.expire_all()
    disabled = db.get(User, target.id)
    assert disabled is not None
    assert not disabled.is_active
    assert find_valid_session(db, target_raw_token, touch=False) is None

    enable_response = submit_user_edit(client, disabled, is_active="true")

    assert enable_response.status_code == 303
    db.expire_all()
    enabled = db.get(User, target.id)
    assert enabled is not None
    assert enabled.is_active
    audits = db.scalars(
        select(AuditLog)
        .where(AuditLog.action.in_(("user.disable", "user.enable")))
        .order_by(AuditLog.created_at, AuditLog.action)
    ).all()
    assert {audit.action for audit in audits} == {"user.disable", "user.enable"}
    disable_audit = next(audit for audit in audits if audit.action == "user.disable")
    assert disable_audit.details["sessions_revoked"] == 1


def test_role_change_revokes_all_target_sessions(
    client: TestClient,
    db: Session,
) -> None:
    create_user(db, permissions=("users.manage",))
    operator_role = create_role(db, "operator", "Operator")
    security_role = create_role(db, "security", "Security")
    target = create_staff_user(db, operator_role)
    first_token, _ = create_session(db, target)
    second_token, _ = create_session(db, target)
    db.commit()
    assert login(client).status_code == 303

    response = submit_user_edit(
        client,
        target,
        role_ids=[str(security_role.id)],
    )

    assert response.status_code == 303
    assert find_valid_session(db, first_token, touch=False) is None
    assert find_valid_session(db, second_token, touch=False) is None
    audit = db.scalar(select(AuditLog).where(AuditLog.action == "user.update"))
    assert audit is not None
    assert audit.details["sessions_revoked"] == 2


def test_current_user_cannot_disable_themselves(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(db, permissions=("users.manage",))
    assert login(client).status_code == 303

    response = submit_user_edit(client, actor, is_active="")

    assert response.status_code == 400
    assert "You cannot disable your own account." in response.text
    db.refresh(actor)
    assert actor.is_active
    assert client.get("/dashboard").status_code == 200


@pytest.mark.parametrize("change", ["disable", "remove-role"])
def test_last_active_administrator_is_protected(
    client: TestClient,
    db: Session,
    change: str,
) -> None:
    actor = create_user(db, permissions=("users.manage",))
    actor.roles[0].code = "access-manager"
    actor.roles[0].name = "Access Manager"
    administrator_role = create_role(db, "administrator", "Administrator")
    operator_role = create_role(db, "operator", "Operator")
    target = create_staff_user(
        db,
        administrator_role,
        username="last-admin",
        email="last-admin@example.test",
    )
    db.commit()
    assert login(client).status_code == 303

    overrides: dict[str, object]
    if change == "disable":
        overrides = {"is_active": ""}
    else:
        overrides = {"role_ids": [str(operator_role.id)]}
    response = submit_user_edit(client, target, **overrides)

    assert response.status_code == 400
    assert "last active administrator" in response.text
    db.expire_all()
    unchanged = db.get(User, target.id)
    assert unchanged is not None
    assert unchanged.is_active
    assert {role.code for role in unchanged.roles} == {"administrator"}


@pytest.mark.parametrize("csrf_token", [None, "invalid"])
def test_edit_user_rejects_missing_or_invalid_csrf(
    client: TestClient,
    db: Session,
    csrf_token: str | None,
) -> None:
    create_user(db, permissions=("users.manage",))
    role = create_role(db, "operator", "Operator")
    target = create_staff_user(db, role)
    assert login(client).status_code == 303
    data = {
        "full_name": "Changed Without CSRF",
        "email": target.email or "",
        "role_ids": str(role.id),
        "is_active": "true",
    }
    if csrf_token is not None:
        data["csrf_token"] = csrf_token

    response = client.post(f"/users/{target.id}/edit", data=data)

    assert response.status_code == 403
    db.refresh(target)
    assert target.full_name == "Original Operator"


def submit_password_reset(
    client: TestClient,
    target: User,
    *,
    temporary_password: str = "replacement secure passphrase",
    confirm_password: str = "replacement secure passphrase",
):
    page = client.get(f"/users/{target.id}/edit")
    assert page.status_code == 200
    return client.post(
        f"/users/{target.id}/reset-password",
        data={
            "temporary_password": temporary_password,
            "confirm_password": confirm_password,
            "csrf_token": extract_csrf_token(page.text),
        },
    )


def test_password_reset_and_unlock_require_users_manage(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(db, permissions=("users.view",))
    role = create_role(db, "operator", "Operator")
    target = create_staff_user(db, role)
    original_hash = target.password_hash
    target.failed_login_count = 4
    target.locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
    db.commit()
    assert login(client).status_code == 303
    csrf_token = extract_csrf_token(client.get("/dashboard").text)

    reset_response = client.post(
        f"/users/{target.id}/reset-password",
        data={
            "temporary_password": "replacement secure passphrase",
            "confirm_password": "replacement secure passphrase",
            "csrf_token": csrf_token,
        },
    )
    unlock_response = client.post(
        f"/users/{target.id}/unlock",
        data={"csrf_token": csrf_token},
    )

    assert reset_response.status_code == 403
    assert unlock_response.status_code == 403
    db.refresh(target)
    assert target.password_hash == original_hash
    assert target.locked_until is not None


def test_valid_password_reset_hashes_password_forces_change_and_revokes_sessions(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(db, permissions=("users.manage",))
    role = create_role(db, "operator", "Operator")
    target = create_staff_user(db, role)
    original_hash = target.password_hash
    first_token, _ = create_session(db, target)
    second_token, _ = create_session(db, target)
    db.commit()
    assert login(client).status_code == 303

    response = submit_password_reset(client, target)

    assert response.status_code == 303
    assert response.headers["location"] == f"/users/{target.id}/edit"
    db.expire_all()
    reset_user = db.get(User, target.id)
    assert reset_user is not None
    assert reset_user.password_hash != original_hash
    assert reset_user.password_hash.startswith("$argon2")
    assert verify_password("replacement secure passphrase", reset_user.password_hash)
    assert reset_user.must_change_password
    assert find_valid_session(db, first_token, touch=False) is None
    assert find_valid_session(db, second_token, touch=False) is None

    audit = db.scalar(
        select(AuditLog).where(AuditLog.action == "user.password_reset")
    )
    assert audit is not None
    assert audit.actor_user_id == actor.id
    assert audit.entity_id == str(target.id)
    assert audit.details == {
        "username": target.username,
        "sessions_revoked": 2,
    }
    audit_text = str(audit.details).lower()
    assert "replacement secure passphrase" not in audit_text
    assert reset_user.password_hash not in audit_text
    assert "token" not in audit_text
    assert "csrf" not in audit_text


@pytest.mark.parametrize(
    ("temporary_password", "confirm_password", "message"),
    [
        (
            "short pass",
            "short pass",
            "Temporary password must contain at least 12 characters.",
        ),
        (
            "replacement secure passphrase",
            "different secure passphrase",
            "Temporary passwords do not match.",
        ),
    ],
)
def test_password_reset_rejects_short_or_mismatched_password(
    client: TestClient,
    db: Session,
    temporary_password: str,
    confirm_password: str,
    message: str,
) -> None:
    create_user(db, permissions=("users.manage",))
    role = create_role(db, "operator", "Operator")
    target = create_staff_user(db, role)
    original_hash = target.password_hash
    assert login(client).status_code == 303

    response = submit_password_reset(
        client,
        target,
        temporary_password=temporary_password,
        confirm_password=confirm_password,
    )

    assert response.status_code == 400
    assert message in response.text
    assert temporary_password not in response.text
    db.refresh(target)
    assert target.password_hash == original_hash
    assert db.scalar(
        select(AuditLog).where(AuditLog.action == "user.password_reset")
    ) is None


def test_reset_user_can_login_and_is_forced_to_change_password(
    client: TestClient,
    db: Session,
) -> None:
    create_user(db, permissions=("users.manage",))
    role = create_role(db, "operator", "Operator")
    target = create_staff_user(db, role)
    assert login(client).status_code == 303
    assert submit_password_reset(client, target).status_code == 303

    edit_page = client.get(f"/users/{target.id}/edit")
    logout_token = extract_csrf_token(edit_page.text)
    assert client.post("/logout", data={"csrf_token": logout_token}).status_code == 303

    old_password_response = login(
        client,
        username=target.username,
        password="operator secure passphrase",
    )
    reset_password_response = login(
        client,
        username=target.username,
        password="replacement secure passphrase",
    )

    assert old_password_response.status_code == 401
    assert reset_password_response.status_code == 303
    assert reset_password_response.headers["location"] == "/account/change-password"
    assert client.get("/dashboard").headers["location"] == "/account/change-password"


def test_unlock_clears_lock_state_and_audits_without_changing_password(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(db, permissions=("users.manage",))
    role = create_role(db, "operator", "Operator")
    target = create_staff_user(db, role)
    target.failed_login_count = 5
    target.locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
    original_hash = target.password_hash
    db.commit()
    assert login(client).status_code == 303
    page = client.get(f"/users/{target.id}/edit")

    response = client.post(
        f"/users/{target.id}/unlock",
        data={"csrf_token": extract_csrf_token(page.text)},
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/users/{target.id}/edit"
    db.refresh(target)
    assert target.failed_login_count == 0
    assert target.locked_until is None
    assert target.password_hash == original_hash
    audit = db.scalar(select(AuditLog).where(AuditLog.action == "user.unlock"))
    assert audit is not None
    assert audit.actor_user_id == actor.id
    assert audit.entity_id == str(target.id)
    assert audit.details == {"username": target.username}
    assert original_hash not in str(audit.details)


@pytest.mark.parametrize(
    ("action", "csrf_token"),
    [
        ("reset-password", None),
        ("reset-password", "invalid"),
        ("unlock", None),
        ("unlock", "invalid"),
    ],
)
def test_password_reset_and_unlock_reject_missing_or_invalid_csrf(
    client: TestClient,
    db: Session,
    action: str,
    csrf_token: str | None,
) -> None:
    create_user(db, permissions=("users.manage",))
    role = create_role(db, "operator", "Operator")
    target = create_staff_user(db, role)
    target.failed_login_count = 5
    target.locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
    original_hash = target.password_hash
    db.commit()
    assert login(client).status_code == 303
    data = {
        "temporary_password": "replacement secure passphrase",
        "confirm_password": "replacement secure passphrase",
    }
    if csrf_token is not None:
        data["csrf_token"] = csrf_token

    response = client.post(f"/users/{target.id}/{action}", data=data)

    assert response.status_code == 403
    db.refresh(target)
    assert target.password_hash == original_hash
    assert target.failed_login_count == 5
    assert target.locked_until is not None


def test_administrator_cannot_reset_own_password(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(db, permissions=("users.manage",))
    original_hash = actor.password_hash
    assert login(client).status_code == 303

    response = client.post(
        f"/users/{actor.id}/reset-password",
        data={
            "temporary_password": "replacement secure passphrase",
            "confirm_password": "replacement secure passphrase",
            "csrf_token": extract_csrf_token(
                client.get(f"/users/{actor.id}/edit").text
            ),
        },
    )

    assert response.status_code == 400
    assert "Use Change Password" in response.text
    db.refresh(actor)
    assert actor.password_hash == original_hash


@pytest.mark.parametrize("path", ["/forgot-password", "/reset-password"])
def test_public_password_reset_routes_do_not_exist(
    client: TestClient,
    path: str,
) -> None:
    assert client.get(path).status_code == 404
    login_page = client.get("/login")
    assert f'href="{path}"' not in login_page.text


def test_audit_page_redirects_anonymous_user(client: TestClient) -> None:
    response = client.get("/audit")

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Faudit"


def test_audit_page_requires_audit_view_permission(
    client: TestClient,
    db: Session,
) -> None:
    create_user(db, permissions=("users.view",))
    assert login(client).status_code == 303

    response = client.get("/audit")

    assert response.status_code == 403
    assert response.json() == {"detail": "Permission denied"}


def test_audit_page_renders_real_records(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(db, permissions=("audit.view",))
    create_audit_log(
        db,
        action="camera.configuration_test",
        actor_user_id=actor.id,
        entity_type="camera",
        entity_id="MAIN-ENTRY-01",
        details={"result": "audit-record-visible", "changed": 2},
        ip_address="192.0.2.44",
    )
    db.commit()
    assert login(client).status_code == 303

    response = client.get("/audit")

    assert response.status_code == 200
    assert "Audit events" in response.text
    assert "camera.configuration_test" in response.text
    assert "admin · System Administrator" in response.text
    assert "MAIN-ENTRY-01" in response.text
    assert "192.0.2.44" in response.text
    assert "result=audit-record-visible" in response.text
    assert "changed=2" in response.text
    assert 'data-nav-key="audit"' in response.text
    assert 'href="/audit"' in response.text


def test_audit_filters_action_actor_and_date_range(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(db, permissions=("audit.view",))
    role = create_role(db, "operator", "Operator")
    other_actor = create_staff_user(
        db,
        role,
        username="audit-operator",
        email="audit-operator@example.test",
    )
    db.add_all(
        [
            AuditLog(
                actor_user_id=actor.id,
                action="camera.enable",
                entity_type="camera",
                entity_id="CAM-A",
                details={"marker": "ACTION-FILTER-MARKER"},
                created_at=datetime(2026, 1, 10, 12, tzinfo=timezone.utc),
            ),
            AuditLog(
                actor_user_id=other_actor.id,
                action="user.update",
                entity_type="user",
                entity_id=str(other_actor.id),
                details={"marker": "ACTOR-DATE-FILTER-MARKER"},
                created_at=datetime(2026, 2, 10, 12, tzinfo=timezone.utc),
            ),
            AuditLog(
                actor_user_id=other_actor.id,
                action="camera.disable",
                entity_type="camera",
                entity_id="CAM-C",
                details={"marker": "ACTOR-ONLY-MARKER"},
                created_at=datetime(2026, 3, 10, 12, tzinfo=timezone.utc),
            ),
        ]
    )
    db.commit()
    assert login(client).status_code == 303

    action_response = client.get("/audit", params={"action": "camera.enable"})
    actor_response = client.get("/audit", params={"actor": str(other_actor.id)})
    actor_name_response = client.get("/audit", params={"actor": "AUDIT-OPERATOR"})
    date_response = client.get(
        "/audit",
        params={"date_from": "2026-02-01", "date_to": "2026-02-28"},
    )

    assert action_response.status_code == 200
    assert "ACTION-FILTER-MARKER" in action_response.text
    assert "ACTOR-DATE-FILTER-MARKER" not in action_response.text
    assert "ACTOR-ONLY-MARKER" not in action_response.text

    for response in (actor_response, actor_name_response):
        assert response.status_code == 200
        assert "ACTOR-DATE-FILTER-MARKER" in response.text
        assert "ACTOR-ONLY-MARKER" in response.text
        assert "ACTION-FILTER-MARKER" not in response.text

    assert date_response.status_code == 200
    assert "ACTOR-DATE-FILTER-MARKER" in date_response.text
    assert "ACTION-FILTER-MARKER" not in date_response.text
    assert "ACTOR-ONLY-MARKER" not in date_response.text
    assert "2026-02-10 20:00:00" in date_response.text


def test_audit_viewer_removes_sensitive_details_at_read_time(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(db, permissions=("audit.view",))
    db.add(
        AuditLog(
            actor_user_id=actor.id,
            action="legacy.unsafe_record",
            details={
                "safe_note": "VISIBLE-SAFE-MARKER",
                "password": "LEAKED-PASSWORD-VALUE",
                "password_hash": "LEAKED-HASH-VALUE",
                "session_token": "LEAKED-SESSION-VALUE",
                "csrf_token": "LEAKED-CSRF-VALUE",
                "secret_key": "LEAKED-SECRET-VALUE",
                "nested": {
                    "authorization": "LEAKED-AUTH-VALUE",
                    "safe": "VISIBLE-NESTED-MARKER",
                },
            },
        )
    )
    db.commit()
    assert login(client).status_code == 303

    response = client.get("/audit")

    assert response.status_code == 200
    assert "VISIBLE-SAFE-MARKER" in response.text
    assert "VISIBLE-NESTED-MARKER" in response.text
    for sensitive_value in (
        "LEAKED-PASSWORD-VALUE",
        "LEAKED-HASH-VALUE",
        "LEAKED-SESSION-VALUE",
        "LEAKED-CSRF-VALUE",
        "LEAKED-SECRET-VALUE",
        "LEAKED-AUTH-VALUE",
    ):
        assert sensitive_value not in response.text


@pytest.mark.parametrize(
    ("permissions", "audit_visible"),
    [
        (("audit.view",), True),
        (("users.view",), False),
    ],
)
def test_sidebar_visibility_respects_audit_view_permission(
    client: TestClient,
    db: Session,
    permissions: tuple[str, ...],
    audit_visible: bool,
) -> None:
    create_user(db, permissions=permissions)
    assert login(client).status_code == 303

    dashboard = client.get("/dashboard")

    assert dashboard.status_code == 200
    assert ('data-nav-key="audit"' in dashboard.text) is audit_visible
    assert ('href="/audit"' in dashboard.text) is audit_visible


def create_site(db: Session) -> Site:
    site = Site(
        code="MAIN",
        name="Main Site",
        timezone="Asia/Kuala_Lumpur",
        is_active=True,
    )
    db.add(site)
    db.commit()
    return site


def submit_new_camera(
    client: TestClient,
    site: Site,
    **overrides: object,
):
    page = client.get("/cameras/new")
    assert page.status_code == 200
    data: dict[str, object] = {
        "site_id": str(site.id),
        "code": "MAIN-ENTRY-01",
        "name": "Main Entry",
        "direction": "ENTRY",
        "manufacturer": "Neutral Vision",
        "model": "NV-RA5",
        "serial_number": "SN-10001",
        "ip_address": "192.0.2.10",
        "is_enabled": "true",
        "csrf_token": extract_csrf_token(page.text),
    }
    data.update(overrides)
    return client.post("/cameras/new", data=data)


def test_camera_pages_redirect_anonymous_user(
    client: TestClient,
    db: Session,
) -> None:
    site = create_site(db)
    camera = Camera(
        site_id=site.id,
        code="MAIN-ENTRY-01",
        name="Main Entry",
        direction="ENTRY",
    )
    db.add(camera)
    db.commit()

    list_response = client.get("/cameras")
    detail_response = client.get(f"/cameras/{camera.id}")

    assert list_response.status_code == 303
    assert list_response.headers["location"] == "/login?next=%2Fcameras"
    assert detail_response.status_code == 303


def test_camera_view_and_manage_permissions_are_enforced(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(db, permissions=("cameras.view",))
    site = create_site(db)
    camera = Camera(
        site_id=site.id,
        code="MAIN-ENTRY-01",
        name="Main Entry",
        direction="ENTRY",
    )
    db.add(camera)
    db.commit()
    assert login(client).status_code == 303

    camera_list = client.get("/cameras")
    detail = client.get(f"/cameras/{camera.id}")
    create_page = client.get("/cameras/new")
    csrf_token = extract_csrf_token(detail.text)
    create_response = client.post(
        "/cameras/new",
        data={
            "site_id": str(site.id),
            "code": "SECOND-CAMERA",
            "name": "Second Camera",
            "direction": "EXIT",
            "csrf_token": csrf_token,
        },
    )
    disable_response = client.post(
        f"/cameras/{camera.id}/disable",
        data={"csrf_token": csrf_token},
    )

    assert camera_list.status_code == 200
    assert detail.status_code == 200
    assert create_page.status_code == 403
    assert create_response.status_code == 403
    assert disable_response.status_code == 403
    assert 'href="/cameras/new"' not in camera_list.text
    assert actor.username in detail.text


def test_camera_pages_deny_user_without_cameras_view(
    client: TestClient,
    db: Session,
) -> None:
    create_user(db, permissions=("audit.view",))
    site = create_site(db)
    camera = Camera(
        site_id=site.id,
        code="MAIN-ENTRY-01",
        name="Main Entry",
        direction="ENTRY",
    )
    db.add(camera)
    db.commit()
    assert login(client).status_code == 303

    assert client.get("/cameras").status_code == 403
    assert client.get(f"/cameras/{camera.id}").status_code == 403


def test_create_camera_stores_configuration_unprovisioned_state_and_audit(
    client: TestClient,
    db: Session,
) -> None:
    actor = create_user(
        db,
        permissions=("cameras.view", "cameras.manage"),
    )
    site = create_site(db)
    assert login(client).status_code == 303

    response = submit_new_camera(client, site)

    assert response.status_code == 303
    camera = db.scalar(select(Camera).where(Camera.code == "MAIN-ENTRY-01"))
    assert camera is not None
    assert response.headers["location"] == f"/cameras/{camera.id}"
    assert isinstance(camera.id, uuid.UUID)
    assert camera.site_id == site.id
    assert camera.name == "Main Entry"
    assert camera.direction == "ENTRY"
    assert camera.manufacturer == "Neutral Vision"
    assert camera.model == "NV-RA5"
    assert camera.serial_number == "SN-10001"
    assert camera.ip_address == "192.0.2.10"
    assert camera.is_enabled
    assert camera.provisioning_state == UNPROVISIONED
    assert CameraService(db).camera_detail(camera).connection_state == UNPROVISIONED

    audit = db.scalar(select(AuditLog).where(AuditLog.action == "camera.create"))
    assert audit is not None
    assert audit.actor_user_id == actor.id
    assert audit.entity_id == str(camera.id)
    assert audit.site_id == site.id
    assert audit.details["provisioning_state"] == UNPROVISIONED
    assert "credential" not in str(audit.details).lower()
    assert "secret" not in str(audit.details).lower()

    camera_list = client.get("/cameras")
    assert camera_list.status_code == 200
    assert "MAIN-ENTRY-01" in camera_list.text
    assert "Neutral Vision / NV-RA5" in camera_list.text
    assert "192.0.2.10" in camera_list.text
    assert "Never" in camera_list.text
    assert 'href="/cameras/new"' in camera_list.text

    detail = client.get(f"/cameras/{camera.id}")
    assert detail.status_code == 200
    assert 'status-badge--unprovisioned">UNPROVISIONED' in detail.text
    assert "Never" in detail.text
    assert f"anpr/{camera.id}/status" in detail.text
    assert f"anpr/{camera.id}/event" in detail.text
    assert f"anpr/{camera.id}/command" in detail.text
    assert f"anpr/{camera.id}/whitelist" in detail.text
    assert ">Connect</button>" not in detail.text
    assert ">Reconnect</button>" not in detail.text


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"code": "main-entry-01", "serial_number": "SN-OTHER"}, "That camera code is already in use."),
        ({"code": "MAIN-EXIT-02", "serial_number": "sn-10001"}, "That serial number is already in use."),
        ({"code": "MAIN-EXIT-02", "serial_number": "SN-OTHER", "ip_address": "999.1.2.3"}, "Enter a valid IP address."),
        ({"code": "MAIN-EXIT-02", "serial_number": "SN-OTHER", "site_id": "not-a-site"}, "Select a valid site."),
    ],
)
def test_create_camera_rejects_duplicates_invalid_ip_and_invalid_site(
    client: TestClient,
    db: Session,
    overrides: dict[str, object],
    message: str,
) -> None:
    create_user(db, permissions=("cameras.manage",))
    site = create_site(db)
    db.add(
        Camera(
            site_id=site.id,
            code="MAIN-ENTRY-01",
            name="Existing Camera",
            direction="ENTRY",
            serial_number="SN-10001",
        )
    )
    db.commit()
    assert login(client).status_code == 303

    response = submit_new_camera(client, site, **overrides)

    assert response.status_code == 400
    assert message in response.text
    assert db.scalar(select(Camera).where(Camera.code == "MAIN-EXIT-02")) is None


def test_camera_disable_enable_updates_derived_state_and_audits(
    client: TestClient,
    db: Session,
) -> None:
    create_user(db, permissions=("cameras.view", "cameras.manage"))
    site = create_site(db)
    camera = Camera(
        site_id=site.id,
        code="MAIN-ENTRY-01",
        name="Main Entry",
        direction="ENTRY",
        is_enabled=True,
    )
    db.add(camera)
    db.commit()
    assert login(client).status_code == 303

    detail = client.get(f"/cameras/{camera.id}")
    disable_response = client.post(
        f"/cameras/{camera.id}/disable",
        data={"csrf_token": extract_csrf_token(detail.text)},
    )

    assert disable_response.status_code == 303
    db.refresh(camera)
    assert not camera.is_enabled
    assert CameraService(db).camera_detail(camera).connection_state == DISABLED

    detail = client.get(f"/cameras/{camera.id}")
    enable_response = client.post(
        f"/cameras/{camera.id}/enable",
        data={"csrf_token": extract_csrf_token(detail.text)},
    )

    assert enable_response.status_code == 303
    db.refresh(camera)
    assert camera.is_enabled
    assert CameraService(db).camera_detail(camera).connection_state == UNPROVISIONED
    actions = set(
        db.scalars(
            select(AuditLog.action).where(
                AuditLog.action.in_(("camera.disable", "camera.enable"))
            )
        ).all()
    )
    assert actions == {"camera.disable", "camera.enable"}


def test_camera_provisioning_revoke_is_real_and_does_not_fake_connectivity(
    client: TestClient,
    db: Session,
) -> None:
    create_user(db, permissions=("cameras.view", "cameras.manage"))
    site = create_site(db)
    camera = Camera(
        site_id=site.id,
        code="MAIN-ENTRY-01",
        name="Main Entry",
        direction="ENTRY",
        is_enabled=True,
    )
    db.add(camera)
    db.commit()
    assert login(client).status_code == 303
    detail = client.get(f"/cameras/{camera.id}")

    response = client.post(
        f"/cameras/{camera.id}/provisioning/revoke",
        data={"csrf_token": extract_csrf_token(detail.text)},
    )

    assert response.status_code == 303
    db.refresh(camera)
    assert camera.provisioning_state == CREDENTIALS_REVOKED
    assert CameraService(db).camera_detail(camera).connection_state == CREDENTIALS_REVOKED
    audit = db.scalar(
        select(AuditLog).where(
            AuditLog.action == "camera.provisioning_revoke"
        )
    )
    assert audit is not None
    audit_text = str(audit.details).lower()
    assert "password" not in audit_text
    assert "secret" not in audit_text
    assert "token" not in audit_text
    assert "hash" not in audit_text
    revoked_detail = client.get(f"/cameras/{camera.id}")
    assert "CREDENTIALS_REVOKED" in revoked_detail.text
    assert "Re-provision unavailable" in revoked_detail.text


@pytest.mark.parametrize(
    ("path_suffix", "form_data"),
    [
        ("disable", {}),
        ("disable", {"csrf_token": "invalid"}),
        ("provisioning/revoke", {}),
        ("provisioning/revoke", {"csrf_token": "invalid"}),
    ],
)
def test_camera_actions_reject_missing_or_invalid_csrf(
    client: TestClient,
    db: Session,
    path_suffix: str,
    form_data: dict[str, str],
) -> None:
    create_user(db, permissions=("cameras.manage",))
    site = create_site(db)
    camera = Camera(
        site_id=site.id,
        code="MAIN-ENTRY-01",
        name="Main Entry",
        direction="ENTRY",
        is_enabled=True,
    )
    db.add(camera)
    db.commit()
    assert login(client).status_code == 303

    response = client.post(
        f"/cameras/{camera.id}/{path_suffix}",
        data=form_data,
    )

    assert response.status_code == 403
    db.refresh(camera)
    assert camera.is_enabled
    assert camera.provisioning_state == UNPROVISIONED


@pytest.mark.parametrize("csrf_token", [None, "invalid"])
def test_camera_creation_rejects_missing_or_invalid_csrf(
    client: TestClient,
    db: Session,
    csrf_token: str | None,
) -> None:
    create_user(db, permissions=("cameras.manage",))
    site = create_site(db)
    assert login(client).status_code == 303
    data = {
        "site_id": str(site.id),
        "code": "MAIN-ENTRY-01",
        "name": "Main Entry",
        "direction": "ENTRY",
    }
    if csrf_token is not None:
        data["csrf_token"] = csrf_token

    response = client.post("/cameras/new", data=data)

    assert response.status_code == 403
    assert db.scalar(select(Camera)) is None


@pytest.mark.parametrize(
    ("permissions", "cameras_visible"),
    [
        (("cameras.view",), True),
        (("audit.view",), False),
    ],
)
def test_sidebar_visibility_respects_cameras_view_permission(
    client: TestClient,
    db: Session,
    permissions: tuple[str, ...],
    cameras_visible: bool,
) -> None:
    create_user(db, permissions=permissions)
    assert login(client).status_code == 303

    dashboard = client.get("/dashboard")

    assert dashboard.status_code == 200
    assert ('data-nav-key="cameras"' in dashboard.text) is cameras_visible
    assert ('href="/cameras"' in dashboard.text) is cameras_visible
