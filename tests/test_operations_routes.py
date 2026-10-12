import re
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password
from app.db.dependencies import get_db
from app.main import app
from app.models.audit_log import AuditLog
from app.models.barrier import BarrierAction
from app.models.camera import Camera
from app.models.operations import ParkingReconciliation, RecognitionPlateCorrection, VehicleWatch
from app.models.parking import AccessDecision, ParkingSession, ParkingSessionIssue
from app.models.permission import Permission
from app.models.recognition_event import RecognitionEvent
from app.models.role import Role
from app.models.site import Site
from app.models.user import User
from app.models.vehicle import Vehicle
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


def create_user(db: Session, permissions: tuple[str, ...]) -> User:
    role = Role(code="operator", name="Operator", is_system=True)
    role.permissions = [Permission(code=code, description=code) for code in permissions]
    user = User(username="operator", full_name="Lane Operator", password_hash=hash_password(PASSWORD), is_active=True, must_change_password=False, failed_login_count=0, roles=[role])
    db.add(user); db.commit(); return user


def seed(db: Session):
    site = Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur", is_active=True)
    entry = Camera(site=site, code="MAIN-ENTRY", name="Main Entry", direction="ENTRY", lane_role="ENTRY", serial_number="entry-sn", is_enabled=True, connection_state="ONLINE", provisioning_state="PROVISIONED")
    exit_camera = Camera(site=site, code="MAIN-EXIT", name="Main Exit", direction="EXIT", lane_role="EXIT", serial_number="exit-sn", is_enabled=True, connection_state="ONLINE", provisioning_state="PROVISIONED")
    db.add_all((site, entry, exit_camera)); db.commit(); return site, entry, exit_camera


def event(db: Session, camera: Camera, plate="ABC1234", *, at=None, source="LIVE"):
    at = at or datetime.now(timezone.utc)
    item = RecognitionEvent(camera_id=camera.id, site_id=camera.site_id, plate_text=plate, source=source, confidence=98, captured_at=at, received_at=at, event_type="FULL")
    db.add(item); db.commit(); return item


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def login(client: TestClient) -> None:
    page = client.get("/login")
    assert client.post("/login", data={"username": "operator", "password": PASSWORD, "csrf_token": csrf(page.text), "next": "/operations"}).status_code == 303


def test_operations_console_permission_filters_and_missing_images(client: TestClient, db: Session) -> None:
    assert client.get("/operations").status_code == 303
    create_user(db, ("operations.view",)); site, entry, _ = seed(db); recognized = event(db, entry)
    login(client)
    page = client.get("/operations", params={"site": str(site.id), "camera": str(entry.id), "lane_role": "ENTRY", "decision": "", "event": str(recognized.id)})
    assert page.status_code == 200
    assert "Lane Operations" in page.text and "Recent Lane Events" in page.text
    assert "Scene snapshot unavailable" in page.text and "Plate crop unavailable" in page.text
    assert "Raw plate" in page.text and "Effective plate" in page.text
    assert "PREVIEW ONLY" not in page.text
    assert "Movement direction</dt><dd>Unknown" in page.text
    assert "LIVE MODE" in page.text and "Auto-follow latest is on" in page.text
    assert "Correct Plate" not in page.text and "Open Barrier" not in page.text
    assert client.get("/api/operations/recent").status_code == 200


def test_operations_date_filters_and_blank_query_values(client: TestClient, db: Session) -> None:
    create_user(db, ("operations.view",)); site, entry, _ = seed(db)
    event(db, entry, "OLD123", at=datetime(2026, 10, 1, 12, tzinfo=timezone.utc))
    recent = event(db, entry, "NEW123", at=datetime(2026, 10, 11, 12, tzinfo=timezone.utc))
    login(client)

    page = client.get("/operations", params={"site": str(site.id), "camera": "", "date_from": "2026-10-11", "date_to": "2026-10-11"})
    assert page.status_code == 200
    assert "NEW123" in page.text and "OLD123" not in page.text
    payload = client.get("/api/operations/recent", params={"site": str(site.id), "camera": "", "date_from": "2026-10-11", "date_to": "2026-10-11"}).json()
    assert [item["id"] for item in payload["events"]] == [str(recent.id)]
    invalid = client.get("/operations", params={"date_from": "2026-10-12", "date_to": "2026-10-11"})
    assert invalid.status_code == 400 and "From date must not be later" in invalid.text


def test_plate_correction_route_enforces_permission_csrf_and_audits(client: TestClient, db: Session) -> None:
    create_user(db, ("operations.view", "recognition.correct")); _, entry, _ = seed(db); recognized = event(db, entry, "WQT4I20")
    decision = AccessDecision(recognition_event_id=recognized.id, site_id=recognized.site_id, camera_id=entry.id, access_status="NOT_LISTED", decision="REVIEW", reason_code="UNKNOWN", policy_type="RESIDENTIAL", policy_version=1, evaluated_at=recognized.captured_at, response_status="NOT_CONFIGURED", barrier_intent="NONE", physical_execution="NOT_IMPLEMENTED")
    db.add(decision); db.commit(); login(client)
    page = client.get(f"/operations/events/{recognized.id}")
    assert "Correct Plate" in page.text
    url = f"/operations/events/{recognized.id}/correct"
    assert client.post(url, data={"corrected_plate": "WQT4120", "reason": "OCR character misread"}).status_code == 403
    response = client.post(url, data={"csrf_token": csrf(page.text), "corrected_plate": "wqt4120", "reason": "OCR character misread"})
    assert response.status_code == 303
    db.refresh(recognized); db.refresh(decision)
    correction = db.scalar(select(RecognitionPlateCorrection))
    assert recognized.plate_text == "WQT4I20" and correction.corrected_plate_normalized == "WQT4120"
    assert decision.decision == "REVIEW"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "recognition.plate_corrected")) is not None
    assert db.scalar(select(BarrierAction)) is None and db.scalar(select(ParkingSession)) is None
    corrected_page = client.get(f"/operations/events/{recognized.id}")
    assert "PREVIEW ONLY" in corrected_page.text
    assert "Camera read <strong>WQT4I20</strong>" in corrected_page.text


def test_historical_event_is_clearly_distinct_from_live_mode(client: TestClient, db: Session) -> None:
    create_user(db, ("operations.view",)); _, entry, _ = seed(db)
    older = event(db, entry, "OLD123", at=datetime.now(timezone.utc) - timedelta(hours=2))
    event(db, entry, "NEW123", at=datetime.now(timezone.utc))
    login(client)

    page = client.get(f"/operations/events/{older.id}")

    assert page.status_code == 200
    assert "HISTORICAL EVENT" in page.text and "Return to Live" in page.text
    assert "Auto-follow latest is on" not in page.text


def test_reconciliation_route_is_independent_and_audited(client: TestClient, db: Session) -> None:
    create_user(db, ("operations.view", "parking.reconcile")); site, entry_camera, exit_camera = seed(db)
    vehicle = Vehicle(plate_text="ABC1234"); db.add(vehicle); db.flush()
    entered = datetime.now(timezone.utc) - timedelta(hours=1)
    entry = event(db, entry_camera, at=entered)
    exit_event = event(db, exit_camera, at=entered + timedelta(minutes=30))
    session = ParkingSession(site_id=site.id, vehicle_id=vehicle.id, entry_recognition_id=entry.id, status="OPEN", entered_at=entered, source="LIVE")
    issue = ParkingSessionIssue(site_id=site.id, vehicle_id=vehicle.id, recognition_event_id=exit_event.id, issue_type="EXIT_WITHOUT_ENTRY", status="OPEN")
    db.add_all((session, issue)); db.commit(); login(client)
    page = client.get(f"/operations/events/{exit_event.id}")
    token = csrf(page.text)
    url = f"/operations/events/{exit_event.id}/reconcile"
    assert client.post(url, data={"action_type": "LINK_EXIT_TO_OPEN_SESSION", "action_token": "route-link", "session_id": str(session.id), "reason": "Matched manually"}).status_code == 403
    response = client.post(url, data={"csrf_token": token, "action_type": "LINK_EXIT_TO_OPEN_SESSION", "action_token": "route-link", "session_id": str(session.id), "reason": "Matched manually"})
    assert response.status_code == 303
    db.refresh(session); assert session.status == "CLOSED"
    assert db.scalar(select(ParkingReconciliation)) is not None
    assert db.scalar(select(AuditLog).where(AuditLog.action == "parking.reconciliation_applied")) is not None
    assert db.scalar(select(BarrierAction)) is None


def test_watch_routes_keep_history_and_do_not_change_decision(client: TestClient, db: Session) -> None:
    create_user(db, ("operations.view", "vehicle.watch")); site, entry, _ = seed(db); recognized = event(db, entry, "WATCH1")
    decision = AccessDecision(recognition_event_id=recognized.id, site_id=site.id, camera_id=entry.id, access_status="WHITELIST_ACTIVE", decision="ALLOW", reason_code="ACTIVE_WHITELIST", policy_type="RESIDENTIAL", policy_version=1, evaluated_at=recognized.captured_at, response_status="NOT_CONFIGURED", barrier_intent="OPEN", physical_execution="NOT_IMPLEMENTED")
    db.add(decision); db.commit(); login(client)
    page = client.get(f"/operations/events/{recognized.id}")
    response = client.post(f"/operations/events/{recognized.id}/watch", data={"csrf_token": csrf(page.text), "severity": "HIGH_ATTENTION", "note": "Security requested attention"})
    assert response.status_code == 303
    watch = db.scalar(select(VehicleWatch)); db.refresh(decision)
    assert watch.active and decision.decision == "ALLOW" and db.scalar(select(BarrierAction)) is None
    watchlist = client.get("/operations/watchlist")
    assert "High Attention" in watchlist.text and "Security requested attention" in watchlist.text
    assert client.post(f"/operations/watchlist/{watch.id}/clear", data={"csrf_token": csrf(watchlist.text)}).status_code == 303
    db.refresh(watch); assert not watch.active and watch.cleared_at is not None
    actions = set(db.scalars(select(AuditLog.action)).all())
    assert {"vehicle_watch.created", "vehicle_watch.cleared"} <= actions


def test_manual_open_requires_separate_permission_and_reuses_m17_service(client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    create_user(db, ("operations.view",)); _, entry, _ = seed(db); recognized = event(db, entry); login(client)
    page = client.get(f"/operations/events/{recognized.id}")
    assert "Open Barrier" not in page.text
    assert client.post(f"/operations/events/{recognized.id}/open-barrier", data={"csrf_token": csrf(page.text), "reason": "Guard verified", "confirmation": "OPEN GATE"}).status_code == 403


def test_manual_open_with_permission_calls_m17_with_event_context(client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    create_user(db, ("operations.view", "barrier.operate")); _, entry, _ = seed(db); recognized = event(db, entry); login(client)
    observed = []
    monkeypatch.setattr(mqtt_heartbeat_service, "manual_barrier_open", lambda **kwargs: observed.append(kwargs) or uuid4())
    page = client.get(f"/operations/events/{recognized.id}")
    response = client.post(f"/operations/events/{recognized.id}/open-barrier", data={"csrf_token": csrf(page.text), "reason": "Guard verified identity", "confirmation": "OPEN GATE"})
    assert response.status_code == 303
    assert observed[0]["camera_id"] == entry.id and observed[0]["recognition_event_id"] == recognized.id
    assert db.scalar(select(AuditLog).where(AuditLog.action == "barrier.manual_open")) is not None


def test_quick_registration_returns_to_selected_operation_without_opening_gate(client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    create_user(db, ("operations.view", "access_list.manage")); site, entry, _ = seed(db); recognized = event(db, entry, "NEW123")
    monkeypatch.setattr(mqtt_heartbeat_service, "enqueue_access_entry", lambda *_args, **_kwargs: 1)
    login(client)
    operations = client.get(f"/operations/events/{recognized.id}")
    assert "Register Vehicle" in operations.text
    form = client.get("/access-list/new", params={"recognition_id": str(recognized.id), "list_type": "ALLOW", "return_to": f"/operations/events/{recognized.id}"})
    response = client.post("/access-list/new", data={"csrf_token": csrf(form.text), "recognition_id": str(recognized.id), "return_to": f"/operations/events/{recognized.id}", "plate": recognized.plate_text, "site_id": str(site.id), "list_type": "ALLOW", "enabled": "true"})
    assert response.status_code == 303 and response.headers["location"] == f"/operations/events/{recognized.id}"
    assert db.scalar(select(BarrierAction)) is None
