import re
from collections.abc import Generator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password
from app.db.dependencies import get_db
from app.main import app
from app.models.audit_log import AuditLog
from app.models.parking import AccessProfile
from app.models.permission import Permission
from app.models.residential import ResidentialUnit, VisitorReservation
from app.models.role import Role
from app.models.site import Site
from app.models.user import User


PASSWORD = "correct horse battery staple"


@pytest.fixture
def client(db: Session) -> Generator[TestClient, None, None]:
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    def override_db():
        with factory() as session: yield session
    app.dependency_overrides[get_db] = override_db
    with TestClient(app, follow_redirects=False) as test_client: yield test_client
    app.dependency_overrides.clear()


def csrf(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html); assert match
    return match.group(1)


def user(db, permissions):
    role = Role(code="manager", name="Manager", is_system=True)
    role.permissions = [Permission(code=p, description=p) for p in permissions]
    item = User(username="manager", full_name="Property Manager", password_hash=hash_password(PASSWORD), is_active=True, must_change_password=False, failed_login_count=0, roles=[role])
    db.add(item); db.commit(); return item


def login(client):
    page = client.get("/login")
    assert client.post("/login", data={"username": "manager", "password": PASSWORD, "csrf_token": csrf(page.text)}).status_code == 303


def setup_site(db):
    site = Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur", is_active=True); db.add(site); db.flush()
    profile = AccessProfile(site_id=site.id, name="Visitor", enabled=True); db.add(profile); db.commit()
    return site, profile


def test_residential_pages_require_permission_and_navigation_follows_permission(client, db):
    assert client.get("/residential").status_code == 303
    user(db, ("residential.view",)); setup_site(db); login(client)
    page = client.get("/residential")
    assert page.status_code == 200 and "Identity is not authorization" in page.text
    assert 'href="/residential"' in page.text and client.get("/residential/settings").status_code == 403


def test_unit_creation_requires_csrf_and_is_audited(client, db):
    user(db, ("residential.view", "residential.manage")); site, _ = setup_site(db); login(client)
    assert client.post("/residential/units", data={"site_id": str(site.id), "unit_code": "A-12-03"}).status_code == 403
    page = client.get("/residential/units")
    response = client.post("/residential/units", data={"csrf_token": csrf(page.text), "site_id": str(site.id), "unit_code": "A-12-03"})
    assert response.status_code == 303
    assert db.scalar(select(ResidentialUnit).where(ResidentialUnit.unit_code_normalized == "A-12-03")) is not None
    assert db.scalar(select(AuditLog).where(AuditLog.action == "residential.unit_create")) is not None


def test_visitor_manage_cannot_approve_and_approve_never_opens_barrier(client, db):
    user(db, ("residential.view", "visitor.manage")); site, profile = setup_site(db)
    unit = ResidentialUnit(site_id=site.id, unit_code="A-1", unit_code_normalized="A-1", status="ACTIVE"); db.add(unit); db.commit(); login(client)
    page = client.get("/residential/visitors/new"); token = csrf(page.text)
    now = datetime.now(timezone.utc)
    response = client.post("/residential/visitors", data={"csrf_token": token, "site_id": str(site.id), "unit_id": str(unit.id), "visitor_name": "Alice", "plate": "ABC123", "access_profile_id": str(profile.id), "visit_mode": "SINGLE_VISIT", "valid_from": now.strftime("%Y-%m-%dT%H:%M"), "valid_until": (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")})
    item = db.scalar(select(VisitorReservation)); assert response.status_code == 303 and item.approval_status == "PENDING"
    detail = client.get(f"/residential/visitors/{item.id}")
    assert client.post(f"/residential/visitors/{item.id}/approve", data={"csrf_token": csrf(detail.text)}).status_code == 403


def test_scoped_visitor_approver_can_approve_without_access_list_manage(client, db):
    user(db, ("residential.view", "visitor.approve")); site, profile = setup_site(db)
    unit = ResidentialUnit(site_id=site.id, unit_code="B-2", unit_code_normalized="B-2", status="ACTIVE"); db.add(unit); db.flush()
    now = datetime.now(timezone.utc)
    item = VisitorReservation(site_id=site.id, unit_id=unit.id, visitor_name="Bob", plate=None, plate_normalized=None,
        access_profile_id=profile.id, valid_from=now, valid_until=now + timedelta(hours=2), visit_mode="SINGLE_VISIT",
        approval_status="PENDING", visit_state="EXPECTED", access_binding_mode="NONE")
    db.add(item); db.commit(); login(client)
    detail = client.get(f"/residential/visitors/{item.id}")
    response = client.post(f"/residential/visitors/{item.id}/approve", data={"csrf_token": csrf(detail.text)})
    assert response.status_code == 303
    db.refresh(item); assert item.approval_status == "APPROVED" and item.access_list_entry_id is None
    assert client.get("/access-list/new").status_code == 403
