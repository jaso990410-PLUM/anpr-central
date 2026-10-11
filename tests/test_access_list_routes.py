import re
from collections.abc import Generator
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password
from app.db.dependencies import get_db
from app.main import app
from app.models.access_list_entry import AccessListEntry
from app.models.audit_log import AuditLog
from app.models.camera import Camera
from app.models.permission import Permission
from app.models.parking import AccessProfile
from app.models.role import Role
from app.models.site import Site
from app.models.user import User
from app.services.mqtt_service import mqtt_heartbeat_service


PASSWORD = "correct horse battery staple"


@pytest.fixture
def client(db: Session) -> Generator[TestClient, None, None]:
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    def override_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session
    app.dependency_overrides[get_db] = override_db
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def create_user(db: Session, permissions: tuple[str, ...]) -> User:
    role = Role(code="administrator", name="Administrator", is_system=True)
    role.permissions = [Permission(code=code, description=code) for code in permissions]
    user = User(username="admin", full_name="Administrator", password_hash=hash_password(PASSWORD), is_active=True, must_change_password=False, failed_login_count=0, roles=[role])
    db.add(user)
    db.commit()
    return user


def create_site_camera(db: Session) -> tuple[Site, Camera]:
    site = Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur", is_active=True)
    camera = Camera(site=site, code="MAIN-ENTRY-01", name="Main Entry", direction="ENTRY", serial_number="RA5-WEB-TEST", is_enabled=True, access_list_sync_enabled=True)
    db.add(camera)
    db.commit()
    return site, camera


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def login(client: TestClient) -> None:
    page = client.get("/login")
    response = client.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": csrf(page.text), "next": "/dashboard"})
    assert response.status_code == 303


def create_entry(client: TestClient, site: Site, **overrides):
    page = client.get("/access-list/new")
    data = {
        "csrf_token": csrf(page.text), "plate": "wqt4120", "site_id": str(site.id),
        "reference_name": "Nur Aisyah",
        "list_type": "ALLOW", "enabled": "true", "valid_from": "2026-10-04T06:00",
        "valid_until": "2027-10-04T23:00", "daily_window_enabled": "true",
        "daily_start": "06:00:00", "daily_end": "23:00:00", "camera_comment": "Staff",
    }
    data.update(overrides)
    return client.post("/access-list/new", data=data)


def test_access_list_permissions_and_sidebar(client: TestClient, db: Session) -> None:
    assert client.get("/access-list").status_code == 303
    create_user(db, ("access_list.view",))
    create_site_camera(db)
    login(client)
    page = client.get("/access-list")
    assert page.status_code == 200
    assert 'href="/access-list"' in page.text
    assert client.get("/access-list/new").status_code == 403


def test_empty_site_filter_means_all_sites_and_invalid_uuid_is_clear(
    client: TestClient, db: Session
) -> None:
    create_user(db, ("access_list.view",))
    create_site_camera(db)
    login(client)

    assert client.get("/access-list?site=").status_code == 200
    invalid = client.get("/access-list?site=not-a-uuid")
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "Invalid site filter"


def test_create_edit_disable_and_audit_are_operator_friendly(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_user(db, ("access_list.view", "access_list.manage"))
    site, _ = create_site_camera(db)
    queued: list[tuple] = []
    monkeypatch.setattr(mqtt_heartbeat_service, "enqueue_access_entry", lambda entry_id, verify_only=False: queued.append((entry_id, verify_only)) or 1)
    login(client)

    response = create_entry(client, site)
    assert response.status_code == 303
    entry = db.scalar(select(AccessListEntry))
    assert entry is not None and entry.vehicle.plate_text == "WQT4120"
    assert entry.vehicle.reference_name == "Nur Aisyah"
    assert queued == [(entry.id, False)]
    detail = client.get(response.headers["location"])
    assert detail.status_code == 200
    assert "Allowed" in detail.text and "Enabled" in detail.text
    assert "Nur Aisyah" in detail.text
    assert 'name="reference_name"' in detail.text
    assert "Camera Synchronization" in detail.text
    assert "/static/js/access-list.js" in detail.text
    assert "need_alarm" not in detail.text and "time_seg_enable" not in detail.text
    assert "camera index" not in detail.text.lower()
    live = client.get(f"/access-list/{entry.id}/sync-status")
    assert live.status_code == 200
    assert live.json()["cameras"][0]["status"] == "Pending"

    token = csrf(detail.text)
    update = client.post(
        f"/access-list/{entry.id}",
        data={"csrf_token": token, "reference_name": "Visitor 42", "list_type": "BLOCK", "enabled": "true", "daily_window_enabled": "false", "camera_comment": "Review"},
    )
    assert update.status_code == 303
    db.refresh(entry)
    db.refresh(entry.vehicle)
    assert entry.list_type == "BLOCK" and entry.enabled
    assert entry.vehicle.reference_name == "Visitor 42"
    listing = client.get("/access-list")
    assert "Visitor 42" in listing.text

    disable = client.post(f"/access-list/{entry.id}/disable", data={"csrf_token": token})
    assert disable.status_code == 303
    db.refresh(entry)
    assert not entry.enabled and entry.list_type == "BLOCK"
    actions = set(db.scalars(select(AuditLog.action)).all())
    assert {"access_list.create", "access_list.update", "access_list.disable"} <= actions


def test_validation_csrf_and_manual_sync_verify(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_user(db, ("access_list.view", "access_list.manage"))
    site, _ = create_site_camera(db)
    calls: list[bool] = []
    monkeypatch.setattr(mqtt_heartbeat_service, "enqueue_access_entry", lambda _id, verify_only=False: calls.append(verify_only) or 1)
    login(client)
    assert create_entry(client, site, camera_comment="12345678901234567").status_code == 400
    assert client.post("/access-list/new", data={"plate": "ABC1234", "site_id": str(site.id), "list_type": "ALLOW"}).status_code == 403
    created = create_entry(client, site, plate="ABC1234", daily_window_enabled="", daily_start="", daily_end="")
    entry = db.scalar(select(AccessListEntry))
    detail = client.get(created.headers["location"])
    token = csrf(detail.text)
    assert client.post(f"/access-list/{entry.id}/sync", data={"csrf_token": token}).status_code == 303
    assert client.post(f"/access-list/{entry.id}/verify", data={"csrf_token": token}).status_code == 303
    assert calls[-2:] == [False, True]


def test_camera_participation_toggle_is_csrf_protected(client: TestClient, db: Session) -> None:
    create_user(db, ("cameras.view", "cameras.manage"))
    _, camera = create_site_camera(db)
    camera.access_list_sync_enabled = False
    db.commit()
    login(client)
    page = client.get(f"/cameras/{camera.id}")
    assert "Enable access-list synchronization" in page.text
    assert client.post(f"/cameras/{camera.id}/access-list-sync/enable").status_code == 403
    response = client.post(f"/cameras/{camera.id}/access-list-sync/enable", data={"csrf_token": csrf(page.text)})
    assert response.status_code == 303
    db.refresh(camera)
    assert camera.access_list_sync_enabled


def test_access_form_checkboxes_use_compact_component_styles(
    client: TestClient, db: Session
) -> None:
    create_user(db, ("access_list.manage",))
    create_site_camera(db)
    login(client)

    page = client.get("/access-list/new")
    stylesheet = client.get("/static/css/app.css")

    assert 'class="field-group checkbox-field"' in page.text
    assert '.field-group.checkbox-field input[type="checkbox"]' in stylesheet.text
    assert "width: 1rem" in stylesheet.text
    assert "height: 1rem" in stylesheet.text


def test_access_form_uses_operator_friendly_sections_and_exact_datetime_control(
    client: TestClient, db: Session
) -> None:
    create_user(db, ("access_list.manage",))
    site, _ = create_site_camera(db)
    db.add(AccessProfile(
        site_id=site.id,
        name="Contractor",
        description="Time-limited contractor access",
        default_max_stay_minutes=600,
        overstay_monitoring_enabled=True,
        normal_exit_policy="ALLOW",
        expired_exit_policy="REVIEW",
        overstay_exit_policy="DENY",
        blacklist_exit_policy="DENY",
        overstay_exit_grace_minutes=15,
        enabled=True,
    ))
    db.commit()
    login(client)

    page = client.get("/access-list/new")

    assert page.status_code == 200
    for heading in ("Profile &amp; Reference", "Access Control", "Validity &amp; Schedule", "Camera Synchronization"):
        assert heading in page.text
    assert "No access profile" in page.text
    assert "No stay profile" not in page.text
    assert "Owner / Reference Name" in page.text
    assert "profile-specific exit rules" in page.text
    assert 'type="datetime-local" name="valid_from"' in page.text
    assert "Use current date &amp; time" in page.text
    assert 'name="max_stay_override_mode" value="PROFILE"' in page.text
    assert 'name="max_stay_override_unit"' in page.text
    assert 'data-max-stay="600"' in page.text
    assert 'data-expired-exit="REVIEW"' in page.text
    assert "/static/css/access-list.css" in page.text
    assert "/static/js/access-list.js" in page.text


def test_custom_maximum_stay_units_store_minutes_and_profile_default_clears_override(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_user(db, ("access_list.view", "access_list.manage"))
    site, _ = create_site_camera(db)
    monkeypatch.setattr(mqtt_heartbeat_service, "enqueue_access_entry", lambda *_args, **_kwargs: 1)
    login(client)

    created = create_entry(
        client,
        site,
        max_stay_override_mode="CUSTOM",
        max_stay_override_value="10",
        max_stay_override_unit="HOURS",
    )
    assert created.status_code == 303
    entry = db.scalar(select(AccessListEntry))
    assert entry is not None
    assert entry.max_stay_minutes_override == 600

    detail = client.get(created.headers["location"])
    assert 'name="max_stay_override_value" type="number" min="1" step="1" value="10"' in detail.text
    updated = client.post(
        f"/access-list/{entry.id}",
        data={
            "csrf_token": csrf(detail.text),
            "reference_name": "Nur Aisyah",
            "list_type": "ALLOW",
            "enabled": "true",
            "max_stay_override_mode": "PROFILE",
        },
    )
    assert updated.status_code == 303
    db.refresh(entry)
    assert entry.max_stay_minutes_override is None


def test_invalid_custom_maximum_stay_is_rejected(
    client: TestClient, db: Session
) -> None:
    create_user(db, ("access_list.manage",))
    site, _ = create_site_camera(db)
    login(client)

    response = create_entry(
        client,
        site,
        max_stay_override_mode="CUSTOM",
        max_stay_override_value="0",
        max_stay_override_unit="DAYS",
    )

    assert response.status_code == 400
    assert "Maximum-stay override must be greater than zero" in response.text


def test_camera_delete_actions_require_permissions_csrf_and_typed_confirmation(client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    create_user(db, ("access_list.view", "access_list.manage", "cameras.view", "access_list.delete_all_camera"))
    site, camera = create_site_camera(db)
    login(client)
    created = create_entry(client, site, plate="ABC1234")
    entry = db.scalar(select(AccessListEntry))
    detail = client.get(created.headers["location"])
    csrf_value = csrf(detail.text)
    assert "Delete from Camera" in detail.text
    assert "Delete All Camera Access Records" in client.get(f"/cameras/{camera.id}").text
    calls = []
    monkeypatch.setattr(mqtt_heartbeat_service, "delete_camera_access_record", lambda entry_id, camera_id: calls.append(("one", entry_id, camera_id)) or SimpleNamespace(id=uuid4()))
    monkeypatch.setattr(mqtt_heartbeat_service, "delete_all_camera_access_records", lambda camera_id: (calls.append(("all", camera_id)) or SimpleNamespace(id=uuid4()), 1))
    assert client.post(f"/access-list/{entry.id}/cameras/{camera.id}/delete").status_code == 403
    assert client.post(f"/access-list/{entry.id}/cameras/{camera.id}/delete", data={"csrf_token": csrf_value}).status_code == 303
    assert client.post(f"/cameras/{camera.id}/access-list/delete-all", data={"csrf_token": csrf_value, "confirmation": "delete all"}).status_code == 400
    assert client.post(f"/cameras/{camera.id}/access-list/delete-all", data={"csrf_token": csrf_value, "confirmation": "DELETE ALL"}).status_code == 303
    assert calls[0] == ("one", entry.id, camera.id)
    assert calls[1] == ("all", camera.id)
    actions = set(db.scalars(select(AuditLog.action)).all())
    assert {"access_list.camera_delete", "access_list.camera_delete_all"} <= actions


def test_delete_all_permission_is_independent(client: TestClient, db: Session) -> None:
    create_user(db, ("access_list.manage", "cameras.view"))
    _, camera = create_site_camera(db)
    login(client)
    page = client.get(f"/cameras/{camera.id}")
    assert "Delete All Camera Access Records" not in page.text
    assert client.post(f"/cameras/{camera.id}/access-list/delete-all", data={"csrf_token": csrf(page.text), "confirmation": "DELETE ALL"}).status_code == 403
