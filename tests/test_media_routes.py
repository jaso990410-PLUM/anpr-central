from __future__ import annotations

import re
from collections.abc import Generator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password
from app.db.dependencies import get_db
from app.main import app
from app.models.camera import Camera
from app.models.media import MediaSource
from app.models.permission import Permission
from app.models.recognition_event import RecognitionEvent
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


def create_user(db: Session, permissions: tuple[str, ...]):
    role = Role(code="operator", name="Operator", is_system=True)
    role.permissions = [Permission(code=code, description=code) for code in permissions]
    user = User(username="operator", full_name="Media Operator", password_hash=hash_password(PASSWORD),
        is_active=True, must_change_password=False, failed_login_count=0, roles=[role])
    db.add(user); db.commit()


def seed(db: Session):
    site = Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur", is_active=True)
    camera = Camera(site=site, code="MAIN-ENTRY", name="Main Entry", direction="ENTRY", lane_role="ENTRY", is_enabled=True)
    db.add(camera); db.flush()
    at = datetime.now(timezone.utc)
    event = RecognitionEvent(camera_id=camera.id, site_id=site.id, plate_text="VAB1234", source="LIVE",
        confidence=98, captured_at=at, received_at=at, event_type="FULL")
    db.add(event); db.commit(); return camera, event


def csrf(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match; return match.group(1)


def login(client):
    page = client.get("/login")
    response = client.post("/login", data={"username":"operator","password":PASSWORD,"csrf_token":csrf(page.text),"next":"/media"})
    assert response.status_code == 303


def test_media_pages_require_authentication_and_permission(client, db):
    assert client.get("/media").status_code == 303
    create_user(db, ("cameras.view",)); login(client)
    assert client.get("/media").status_code == 403
    assert client.get("/media/storage/folders").status_code == 403


def test_media_overview_and_event_render_truthful_not_configured_state(client, db):
    create_user(db, ("media.view",)); _, event = seed(db); login(client)
    overview = client.get("/media")
    assert overview.status_code == 200 and "ANPR Recording" in overview.text
    page = client.get(f"/media/events/{event.id}")
    assert page.status_code == 200
    assert "NOT CONFIGURED" in page.text and "Continuous recording was not configured" in page.text
    assert "rtsp://" not in page.text


def test_dashboard_live_view_uses_enabled_sources_without_exposing_rtsp(client, db):
    create_user(db, ("media.view",)); camera, _ = seed(db)
    db.add(MediaSource(camera_id=camera.id, provider_type="CENTRAL_RECORDER", enabled=True,
        recording_stream_uri="rtsp://camera.local/main", recorder_state="RECORDING"))
    db.commit(); login(client)

    dashboard = client.get("/dashboard")

    assert dashboard.status_code == 200
    assert "Live Camera View" in dashboard.text
    assert camera.code in dashboard.text
    assert f'/media/cameras/{camera.id}/live-stream' in dashboard.text
    assert "dashboard-live-view.js" in dashboard.text
    assert "rtsp://camera.local/main" not in dashboard.text
    assert 'data-dashboard-live-video' in dashboard.text
    assert 'data-dashboard-live-video src=' not in dashboard.text


def test_dashboard_hides_live_view_without_media_permission(client, db):
    create_user(db, ("cameras.view",)); seed(db); login(client)

    dashboard = client.get("/dashboard")

    assert dashboard.status_code == 200
    assert "Live Camera View" not in dashboard.text
    assert "dashboard-live-view.js" not in dashboard.text


def test_storage_and_source_configuration_enforce_csrf_and_hide_credentials(client, db, tmp_path):
    create_user(db, ("media.view","media.manage","cameras.view")); camera, _ = seed(db); login(client)
    page = client.get("/media"); token = csrf(page.text)
    assert 'id="storage-root"' in page.text and "readonly" in page.text
    assert "Browse Folders" in page.text and "media-storage-picker.js" in page.text
    assert client.post("/media/storage", data={"name":"Primary","root_path":str(tmp_path),"storage_type":"LOCAL","enabled":"true","retention_days":"30","minimum_free_space_gb":"0","minimum_free_space_percent":"0"}).status_code == 403
    created = client.post("/media/storage", data={"csrf_token":token,"name":"Primary","root_path":str(tmp_path),"storage_type":"LOCAL","enabled":"true","retention_days":"30","minimum_free_space_gb":"0","minimum_free_space_percent":"0"})
    assert created.status_code == 303
    page = client.get(f"/cameras/{camera.id}"); token = csrf(page.text)
    bad = client.post(f"/media/cameras/{camera.id}/configure", data={"csrf_token":token,"provider_type":"CENTRAL_RECORDER","recording_stream_uri":"rtsp://admin:secret@camera.local/main","credential_reference":"CAMERA_ONE","pre_event_seconds":"10","post_event_seconds":"15","segment_duration_seconds":"60","media_time_offset_ms":"0","enabled":"true","recording_enabled":"true"})
    assert bad.status_code == 400 and "admin:secret" not in bad.text


def test_server_folder_picker_requires_manage_permission_and_lists_directories_only(client, db, tmp_path):
    create_user(db, ("media.view","media.manage")); login(client)
    child = tmp_path / "Recording"; child.mkdir(); (tmp_path / "secret.txt").write_text("not listed", encoding="utf-8")
    response = client.get("/media/storage/folders", params={"path":str(tmp_path)})
    assert response.status_code == 200
    payload = response.json()
    assert payload["current"] == str(tmp_path.resolve())
    assert payload["directories"] == [{"name":"Recording","path":str(child.resolve())}]
    page = client.get("/media"); token = csrf(page.text)
    assert client.post("/media/storage/folders", data={"parent_path":str(tmp_path),"folder_name":"Created Folder"}).status_code == 403
    created = client.post("/media/storage/folders", data={"csrf_token":token,"parent_path":str(tmp_path),"folder_name":"Created Folder"})
    assert created.status_code == 200 and (tmp_path / "Created Folder").is_dir()
    traversal = client.post("/media/storage/folders", data={"csrf_token":token,"parent_path":str(tmp_path),"folder_name":"../escape"})
    assert traversal.status_code == 400 and not (tmp_path.parent / "escape").exists()
    duplicate = client.post("/media/storage/folders", data={"csrf_token":token,"parent_path":str(tmp_path),"folder_name":"Created Folder"})
    assert duplicate.status_code == 409


def test_export_permission_is_separate(client, db):
    create_user(db, ("media.view",)); _, event = seed(db); login(client)
    page = client.get(f"/media/events/{event.id}")
    assert "Queue Evidence Export" not in page.text
    assert client.post(f"/media/events/{event.id}/export", data={"csrf_token":csrf(page.text)}).status_code == 403
