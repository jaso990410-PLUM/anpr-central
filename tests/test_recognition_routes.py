import base64
import re
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.security import hash_password
from app.db.dependencies import get_db
from app.main import app
from app.models.access_list_camera_sync import AccessListCameraSync
from app.models.access_list_entry import AccessListEntry
from app.models.camera import Camera
from app.models.permission import Permission
from app.models.recognition_event import RecognitionEvent
from app.models.role import Role
from app.models.site import Site
from app.models.user import User
from app.models.vehicle import Vehicle
from app.services.mqtt_service import MqttRuntimeStatus, mqtt_heartbeat_service
from app.services.recognition_media_service import RecognitionMediaService


TEST_PASSWORD = "correct horse battery staple"
PNG = b"\x89PNG\r\n\x1a\nrecognition-test"


@pytest.fixture
def client(db: Session) -> Generator[TestClient, None, None]:
    request_session = sessionmaker(bind=db.get_bind(), expire_on_commit=False)

    def override_get_db() -> Generator[Session, None, None]:
        with request_session() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def create_user(db: Session, permissions: tuple[str, ...]) -> User:
    role = Role(code="operator", name="Operator", is_system=True)
    role.permissions = [
        Permission(code=code, description=f"Test permission: {code}")
        for code in permissions
    ]
    user = User(
        username="operator",
        full_name="Operations User",
        password_hash=hash_password(TEST_PASSWORD),
        is_active=True,
        must_change_password=False,
        failed_login_count=0,
        roles=[role],
    )
    db.add(user)
    db.commit()
    return user


def create_camera(db: Session, *, state: str = "ONLINE") -> Camera:
    site = Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur")
    camera = Camera(
        site=site,
        code="MAIN-ENTRY-01",
        name="Main Entry",
        direction="ENTRY",
        serial_number="RA5-ROUTE-TEST",
        is_enabled=True,
        provisioning_state="PROVISIONED",
        connection_state=state,
        last_seen_at=datetime.now(timezone.utc),
    )
    db.add(camera)
    db.commit()
    return camera


def create_event(
    db: Session,
    camera: Camera,
    *,
    plate: str = "VAB1234",
    received_at: datetime | None = None,
    image_reference: str | None = None,
    source: str = "LIVE",
) -> RecognitionEvent:
    received = received_at or datetime.now(timezone.utc)
    event = RecognitionEvent(
        camera_id=camera.id,
        site_id=camera.site_id,
        vendor_event_id=f"vendor-{plate}",
        plate_text=plate,
        direction="ENTRY",
        confidence=97.4,
        captured_at=received,
        received_at=received,
        event_type="ENRICHED",
        source=source,
        plate_image_path=image_reference,
    )
    db.add(event)
    db.commit()
    return event


def login(client: TestClient) -> None:
    page = client.get("/login")
    token_match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert token_match is not None
    response = client.post(
        "/login",
        data={
            "username": "operator",
            "password": TEST_PASSWORD,
            "csrf_token": token_match.group(1),
            "next": "/dashboard",
        },
    )
    assert response.status_code == 303


def test_recognition_page_redirects_anonymous_and_requires_events_view(
    client: TestClient, db: Session
) -> None:
    assert client.get("/recognition").status_code == 303
    create_user(db, ("cameras.view",))
    login(client)

    assert client.get("/recognition").status_code == 403
    assert client.get("/api/recognitions/live").status_code == 403


def test_recognition_history_filters_and_live_api_are_safe(
    client: TestClient, db: Session
) -> None:
    create_user(db, ("events.view",))
    camera = create_camera(db)
    now = datetime.now(timezone.utc)
    first = create_event(db, camera, plate="VAB1234", received_at=now)
    first.direction = "1"  # legacy vendor motion value must not reach the UI
    first.confidence = 100
    create_event(db, camera, plate="JVV9876", received_at=now - timedelta(days=2))
    create_event(
        db,
        camera,
        plate="REC1234",
        received_at=now - timedelta(hours=1),
        source="OFFLINE_RECOVERY",
    )
    db.commit()
    login(client)

    page = client.get(
        "/recognition",
        params={"plate": "VAB", "camera": str(camera.id), "direction": "ENTRY"},
    )
    assert page.status_code == 200
    assert "VAB1234" in page.text
    assert "JVV9876" not in page.text
    assert "Detected" in page.text
    assert "Complete" in page.text
    assert "ENRICHED" not in page.text
    assert ">Entry<" in page.text
    assert ">1<" not in page.text
    assert "100%" in page.text
    assert "100.0%" not in page.text
    assert 'data-live-recognitions' in page.text
    assert "/static/js/live.js" in page.text
    assert "Source" in page.text

    recovered_page = client.get("/recognition", params={"source": "OFFLINE_RECOVERY"})
    assert recovered_page.status_code == 200
    assert "REC1234" in recovered_page.text
    assert "VAB1234" not in recovered_page.text
    assert ">Recovered<" in recovered_page.text

    all_sources_page = client.get(
        "/recognition",
        params={"camera": "", "source": "", "date_from": "", "date_to": ""},
    )
    assert all_sources_page.status_code == 200
    assert "VAB1234" in all_sources_page.text
    assert "REC1234" in all_sources_page.text

    response = client.get("/api/recognitions/live")
    assert response.status_code == 200
    body = response.json()
    assert any(item["id"] == str(first.id) for item in body["events"])
    rendered = response.text.lower()
    assert "image_path" not in rendered
    assert "event_metadata" not in rendered
    assert "base64" not in rendered
    assert client.get("/api/recognitions/live?after=invalid").status_code == 400


def test_recognition_image_requires_permission_and_uses_validated_media_path(
    client: TestClient,
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "recognition_media_dir", tmp_path)
    create_user(db, ("events.view",))
    camera = create_camera(db)
    event = create_event(db, camera)
    reference = RecognitionMediaService().store(
        event.id,
        "plate",
        base64.b64encode(PNG).decode("ascii"),
    )
    event.plate_image_path = reference
    db.commit()

    assert client.get(f"/recognition/{event.id}/image/plate").status_code == 401
    login(client)
    image = client.get(f"/recognition/{event.id}/image/plate")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content == PNG
    history = client.get("/recognition")
    assert "Plate Crop" in history.text
    assert "data-image-viewer" in history.text
    assert client.get(f"/recognition/{event.id}/image/scene").status_code == 404
    assert client.get(f"/recognition/{event.id}/image/raw").status_code == 422


def test_dashboard_recognition_visibility_follows_events_permission(
    client: TestClient, db: Session
) -> None:
    create_user(db, ("cameras.view",))
    create_camera(db)
    login(client)
    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "Latest recognition" not in dashboard.text
    assert 'href="/recognition"' not in dashboard.text


def test_dashboard_shows_latest_recognition_for_authorized_user(
    client: TestClient, db: Session
) -> None:
    create_user(db, ("events.view",))
    camera = create_camera(db)
    create_event(db, camera, plate="QAB4838E")
    login(client)

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "Latest recognition" in dashboard.text
    assert "QAB4838E" in dashboard.text
    assert "Not Listed" in dashboard.text
    assert 'data-latest-access' in dashboard.text
    assert 'href="/recognition"' in dashboard.text
    assert 'data-latest-recognition' in dashboard.text


def test_recognition_text_is_escaped_and_live_script_uses_safe_dom_apis(
    client: TestClient, db: Session
) -> None:
    create_user(db, ("events.view",))
    camera = create_camera(db)
    create_event(db, camera, plate="<script>alert(1)</script>")
    login(client)

    page = client.get("/recognition")
    script = client.get("/static/js/live.js")
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "textContent" in script.text
    assert "innerHTML" not in script.text


def test_live_camera_status_is_permission_controlled_and_separates_broker_state(
    client: TestClient,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_user(db, ("cameras.view",))
    camera = create_camera(db, state="OFFLINE")
    monkeypatch.setattr(
        mqtt_heartbeat_service,
        "runtime_status",
        lambda: MqttRuntimeStatus(
            is_connected=True,
            is_enabled=True,
            host="192.0.2.20",
            port=1883,
            client_id="anpr-central-test",
            last_connected_at=datetime.now(timezone.utc),
            last_error=None,
        ),
    )
    login(client)

    response = client.get("/api/status/live")
    assert response.status_code == 200
    body = response.json()
    assert body["mqtt"] == {
        "connected": True,
        "enabled": True,
        "broker": "192.0.2.20:1883",
    }
    assert body["cameras_online"] == 0
    assert body["cameras"][0]["id"] == str(camera.id)
    assert body["cameras"][0]["state"] == "OFFLINE"
    assert "client_id" not in response.text


def test_live_camera_status_requires_cameras_view(
    client: TestClient, db: Session
) -> None:
    create_user(db, ("events.view",))
    login(client)
    assert client.get("/api/status/live").status_code == 403


def test_recognition_access_badges_and_controls_follow_permissions(
    client: TestClient, db: Session
) -> None:
    create_user(db, ("events.view",))
    camera = create_camera(db)
    listed = create_event(db, camera, plate="LIST123")
    create_event(db, camera, plate="NONE123")
    vehicle = Vehicle(plate_text="LIST123")
    entry = AccessListEntry(
        vehicle=vehicle,
        site_id=camera.site_id,
        list_type="ALLOW",
        enabled=False,
    )
    db.add(entry)
    db.commit()
    login(client)

    page = client.get("/recognition")
    payload = client.get("/api/recognitions/live").json()

    assert page.status_code == 200
    assert "Whitelist · Disabled" in page.text
    assert "Not Listed" in page.text
    assert "Add to Whitelist" not in page.text
    assert "Edit Access" not in page.text
    live = next(item for item in payload["events"] if item["id"] == str(listed.id))
    assert live["access_status"] == "WHITELIST_DISABLED"
    assert live["access_entry_id"] == str(entry.id)
    assert any(item["id"] == str(listed.id) for item in payload["access_updates"])
    assert "need_alarm" not in client.get("/api/recognitions/live").text


def test_quick_access_registration_prefills_from_event_and_queues_sync(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_user(db, ("events.view", "access_list.manage", "access_list.view"))
    camera = create_camera(db)
    camera.access_list_sync_enabled = True
    event = create_event(db, camera, plate="QAB4838E")
    db.commit()
    enqueued: list[object] = []
    monkeypatch.setattr(
        mqtt_heartbeat_service,
        "enqueue_access_entry",
        lambda entry_id, **_kwargs: enqueued.append(entry_id) or True,
    )
    login(client)

    history = client.get("/recognition")
    assert "Add to Whitelist" in history.text
    assert "Add to Blacklist" in history.text

    form = client.get(
        "/access-list/new",
        params={"recognition_id": str(event.id), "list_type": "BLOCK"},
    )
    assert form.status_code == 200
    assert 'value="QAB4838E"' in form.text
    assert 'value="BLOCK" selected' in form.text
    assert 'name="recognition_id"' in form.text
    assert "Derived from the recognition event" in form.text
    token_match = re.search(r'name="csrf_token" value="([^"]+)"', form.text)
    assert token_match is not None

    response = client.post(
        "/access-list/new",
        data={
            "csrf_token": token_match.group(1),
            "recognition_id": str(event.id),
            "plate": "TAMPERED",
            "site_id": "00000000-0000-0000-0000-000000000000",
            "list_type": "BLOCK",
            "enabled": "true",
            "camera_comment": "history",
        },
    )
    assert response.status_code == 303
    entry = db.scalar(
        select(AccessListEntry)
        .join(Vehicle)
        .where(Vehicle.plate_text == "QAB4838E", AccessListEntry.site_id == camera.site_id)
    )
    assert entry is not None
    assert entry.list_type == "BLOCK"
    assert db.scalar(select(Vehicle).where(Vehicle.plate_text == "TAMPERED")) is None
    assert db.scalar(select(AccessListCameraSync).where(AccessListCameraSync.access_list_entry_id == entry.id)) is not None
    assert enqueued == [entry.id]

    duplicate = client.get(
        "/access-list/new",
        params={"recognition_id": str(event.id), "list_type": "ALLOW"},
    )
    assert duplicate.status_code == 303
    assert duplicate.headers["location"] == f"/access-list/{entry.id}"
    assert len(db.scalars(select(AccessListEntry)).all()) == 1
