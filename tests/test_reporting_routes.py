import re
from collections.abc import Generator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password
from app.db.dependencies import get_db
from app.main import app
from app.models.audit_log import AuditLog
from app.models.permission import Permission
from app.models.role import Role
from app.models.site import Site
from app.models.user import User


PASSWORD = "correct horse battery staple"


@pytest.fixture
def client(db: Session) -> Generator[TestClient, None, None]:
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    def override_db():
        with factory() as session:
            yield session
    app.dependency_overrides[get_db] = override_db
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def create_user(db: Session, permissions: tuple[str, ...]) -> None:
    role = Role(code="reporter", name="Reporter", is_system=True,
                permissions=[Permission(code=value, description=value) for value in permissions])
    db.add(User(username="reporter", full_name="Report User", password_hash=hash_password(PASSWORD),
                is_active=True, must_change_password=False, failed_login_count=0, roles=[role]))
    db.add(Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur", is_active=True))
    db.commit()


def login(client: TestClient) -> None:
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = client.post("/login", data={"username": "reporter", "password": PASSWORD,
                                           "csrf_token": token, "next": "/reports"})
    assert response.status_code == 303


def test_reports_require_authentication_and_view_permission(client: TestClient, db: Session) -> None:
    assert client.get("/reports").status_code == 303
    create_user(db, ())
    login(client)
    assert client.get("/reports").status_code == 403


def test_report_pages_render_aggregates_not_duplicate_record_browsers(client: TestClient, db: Session) -> None:
    create_user(db, ("reports.view",))
    login(client)
    for path, heading in (("/reports", "Reports"), ("/reports/traffic", "Traffic &amp; Occupancy"),
                          ("/reports/access", "Access &amp; Security"),
                          ("/reports/operations", "Operational Activity"),
                          ("/reports/reliability", "System Reliability")):
        response = client.get(path)
        assert response.status_code == 200
        assert heading in response.text
        assert "Historical analytics" in response.text
        assert ">None<" not in response.text and ">null<" not in response.text
    assert "Latest vehicle" not in client.get("/reports").text
    assert "Export CSV" not in client.get("/reports/traffic").text


def test_csv_requires_separate_permission_and_audits_filtered_export(client: TestClient, db: Session) -> None:
    create_user(db, ("reports.view", "reports.export"))
    site = db.scalar(select(Site).where(Site.code == "MAIN"))
    login(client)
    response = client.get("/reports/traffic/export.csv", params={
        "site": str(site.id), "period": "custom", "date_from": "2026-10-15", "date_to": "2026-10-15",
    })
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert "Entry" in response.text and "Asia/Kuala_Lumpur" in response.text
    audit = db.scalar(select(AuditLog).where(AuditLog.action == "report.export"))
    assert audit is not None and audit.details["report"] == "traffic"
    assert "password" not in str(audit.details).lower()


def test_export_permission_is_enforced_directly(client: TestClient, db: Session) -> None:
    create_user(db, ("reports.view",))
    login(client)
    assert client.get("/reports/access/export.csv").status_code == 403


def test_invalid_site_camera_and_range_fail_safely(client: TestClient, db: Session) -> None:
    create_user(db, ("reports.view",))
    login(client)
    assert client.get("/reports", params={"site": "c6da8857-d247-4b11-927d-456e8920a111"}).status_code == 400
    assert client.get("/reports", params={"period": "custom", "date_from": "2026-10-20", "date_to": "2026-10-10"}).status_code == 400
