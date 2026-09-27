import re
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.security import hash_password, hash_session_token
from app.db.dependencies import get_db
from app.main import app
from app.models.audit_log import AuditLog
from app.models.user import User
from app.models.user_session import UserSession


TEST_PASSWORD = "correct horse battery staple"


def create_user(db: Session) -> User:
    user = User(
        username="admin",
        full_name="System Administrator",
        password_hash=hash_password(TEST_PASSWORD),
        is_active=True,
        must_change_password=False,
        failed_login_count=0,
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
    password: str = TEST_PASSWORD,
    next_destination: str = "/dashboard",
):
    login_page = client.get("/login")
    csrf_token = extract_csrf_token(login_page.text)
    return client.post(
        "/login",
        data={
            "username": "admin",
            "password": password,
            "csrf_token": csrf_token,
            "next": next_destination,
        },
    )


def test_login_page_loads_with_secure_form(client: TestClient) -> None:
    response = client.get("/login")

    assert response.status_code == 200
    assert "ANPR Central" in response.text
    assert 'name="username"' in response.text
    assert 'name="password"' in response.text
    assert 'name="csrf_token"' in response.text
    assert settings.csrf_cookie_name in response.cookies


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
    create_user(db)

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
    assert "System Administrator" in dashboard.text

    root = client.get("/")
    assert root.status_code == 303
    assert root.headers["location"] == "/dashboard"


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


def test_logout_rejects_invalid_csrf_token(
    client: TestClient,
    db: Session,
) -> None:
    create_user(db)
    login(client)

    response = client.post(
        "/logout",
        data={"csrf_token": "invalid"},
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
