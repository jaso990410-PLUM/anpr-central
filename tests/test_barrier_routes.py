import re
from collections.abc import Generator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password
from app.db.dependencies import get_db
from app.main import app
from app.models.barrier import BarrierConfiguration
from app.models.camera import Camera
from app.models.permission import Permission
from app.models.role import Role
from app.models.site import Site
from app.models.user import User
from app.services.mqtt_service import mqtt_heartbeat_service


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


def seed(db: Session, permissions):
    role = Role(code="operator", name="Operator", is_system=True)
    role.permissions = [Permission(code=code, description=code) for code in permissions]
    user = User(username="operator", full_name="Operator", password_hash=hash_password(PASSWORD), is_active=True, must_change_password=False, failed_login_count=0, roles=[role])
    site = Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur", is_active=True)
    camera = Camera(site=site, code="MAIN-ENTRY", name="Main Entry", direction="ENTRY", lane_role="ENTRY", serial_number="camera-sn", is_enabled=True, provisioning_state="PROVISIONED", connection_state="ONLINE")
    db.add_all((user, site, camera)); db.commit()
    return camera


def csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client):
    page = client.get("/login")
    assert client.post("/login", data={"username":"operator", "password":PASSWORD, "csrf_token":csrf(page.text), "next":"/dashboard"}).status_code == 303


def test_barrier_page_and_mutations_enforce_permissions_and_csrf(client, db):
    camera = seed(db, ("cameras.view",)); login(client)
    page = client.get(f"/cameras/{camera.id}/barrier")
    assert page.status_code == 200 and "Physical equipment warning" in page.text
    assert "Save configuration" not in page.text
    assert client.post(f"/cameras/{camera.id}/barrier/configuration", data={}).status_code == 403


def test_configuration_and_commissioning_are_explicit_and_audited(client, db):
    camera = seed(db, ("cameras.view", "barrier.manage")); login(client)
    page = client.get(f"/cameras/{camera.id}/barrier")
    assert client.post(f"/cameras/{camera.id}/barrier/configuration", data={"control_mode":"CENTRAL_MANAGED", "feedback_mode":"NONE"}).status_code == 403
    token = csrf(page.text)
    response = client.post(f"/cameras/{camera.id}/barrier/configuration", data={"csrf_token":token, "enabled":"true", "control_mode":"CENTRAL_MANAGED", "feedback_mode":"NONE", "confirmation_timeout":"10", "open_level":"HIGH", "closed_level":"HIGH"})
    assert response.status_code == 303
    config = db.scalar(select(BarrierConfiguration).where(BarrierConfiguration.camera_id == camera.id))
    assert config is not None and config.commissioned_at is None
    page = client.get(f"/cameras/{camera.id}/barrier")
    assert client.post(f"/cameras/{camera.id}/barrier/commission", data={"csrf_token":csrf(page.text), "confirmation":"CENTRAL MANAGED", "local_auto_open_verified":"true"}).status_code == 303
    db.expire_all(); assert db.get(BarrierConfiguration, config.id).commissioned_at is not None


def test_test_open_requires_operate_permission_and_typed_confirmation(client, db, monkeypatch):
    camera = seed(db, ("cameras.view", "barrier.operate")); login(client)
    page = client.get(f"/cameras/{camera.id}/barrier")
    token = csrf(page.text)
    assert client.post(f"/cameras/{camera.id}/barrier/test-open", data={"csrf_token":token, "confirmation":"wrong"}).status_code == 400
    observed = []
    monkeypatch.setattr(mqtt_heartbeat_service, "test_barrier_open", lambda **kwargs: observed.append(kwargs) or uuid4())
    response = client.post(f"/cameras/{camera.id}/barrier/test-open", data={"csrf_token":token, "confirmation":"OPEN GATE"})
    assert response.status_code == 303 and observed[0]["camera_id"] == camera.id


def test_barrier_history_requires_parking_view(client, db):
    seed(db, ("cameras.view",)); login(client)
    assert client.get("/barrier-actions").status_code == 403
