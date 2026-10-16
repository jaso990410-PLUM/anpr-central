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
from app.models.camera import Camera
from app.models.monitoring import ComponentHealth, OperationalAlert, UnifiedEvent
from app.models.parking import AccessDecision
from app.models.permission import Permission
from app.models.recognition_event import RecognitionEvent
from app.models.role import Role
from app.models.site import Site
from app.models.user import User
from app.services.alert_service import AlertService
from app.services.event_policy_service import event_policy
from app.services.health_monitor_service import ComponentHealthService, MonitoringService
from app.services.monitoring_query_service import MonitoringQueryService
from app.services.operational_event_service import EventProjectionService, EventService


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


def user(db: Session, permissions: tuple[str, ...]) -> User:
    role = Role(code="operator", name="Operator", is_system=True,
                permissions=[Permission(code=value, description=value) for value in permissions])
    item = User(username="operator", full_name="Operator", password_hash=hash_password(PASSWORD),
                is_active=True, must_change_password=False, failed_login_count=0, roles=[role])
    db.add(item); db.commit(); return item


def site_camera(db: Session) -> tuple[Site, Camera]:
    site = Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur", is_active=True)
    camera = Camera(site=site, code="MAIN-ENTRY", name="Main Entry", direction="ENTRY", lane_role="ENTRY",
                    is_enabled=True, provisioning_state="PROVISIONED", connection_state="ONLINE",
                    last_seen_at=datetime.now(timezone.utc))
    db.add_all((site, camera)); db.commit(); return site, camera


def recognition(db: Session, camera: Camera, *, source: str = "LIVE", plate: str = "ABC1234") -> RecognitionEvent:
    now = datetime.now(timezone.utc)
    item = RecognitionEvent(camera_id=camera.id, site_id=camera.site_id, plate_text=plate,
                            source=source, captured_at=now, received_at=now, event_type="FULL")
    db.add(item); db.commit(); return item


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def login(client: TestClient) -> None:
    page = client.get("/login")
    response = client.post("/login", data={"username": "operator", "password": PASSWORD,
        "csrf_token": csrf(page.text), "next": "/events"})
    assert response.status_code == 303


def test_event_policy_is_centralized_and_semantically_distinct() -> None:
    normal = event_policy("ANPR_RECOGNITION")
    assert (normal.category, normal.severity, normal.attention_required) == ("TRAFFIC_ANPR", "INFO", False)
    assert event_policy("BLACKLIST_MATCH").attention_required is True
    assert event_policy("MANUAL_BARRIER_RELEASE_REQUESTED").audit_significant is True
    with pytest.raises(ValueError):
        event_policy("HTTP_GET")


def test_projection_is_idempotent_correlated_and_does_not_create_recognition_alert(db: Session) -> None:
    site, camera = site_camera(db)
    event = recognition(db, camera)
    decision = AccessDecision(recognition_event_id=event.id, site_id=site.id, camera_id=camera.id,
        access_status="BLACKLIST", decision="DENY", reason_code="BLACKLIST", policy_type="SITE",
        policy_version=1, evaluated_at=event.captured_at)
    db.add(decision); db.commit()
    service = EventProjectionService(db)
    assert service.project_recent() == 3  # recognition, blacklist match, and deny; no implementation noise
    db.commit()
    assert service.project_recent() == 0
    rows = db.scalars(select(UnifiedEvent).order_by(UnifiedEvent.event_type)).all()
    assert {row.event_type for row in rows} == {"ANPR_RECOGNITION", "BLACKLIST_MATCH", "ACCESS_DENIED"}
    assert all(row.correlation_id == event.id for row in rows)
    assert next(row for row in rows if row.event_type == "ANPR_RECOGNITION").attention_required is False
    assert next(row for row in rows if row.event_type == "BLACKLIST_MATCH").attention_required is True
    assert db.scalar(select(OperationalAlert)) is None


def test_offline_recovery_projection_is_historical_and_non_alerting(db: Session) -> None:
    _, camera = site_camera(db)
    recovered = recognition(db, camera, source="OFFLINE_RECOVERY")
    EventProjectionService(db).project_recent(); db.commit()
    projected = db.scalar(select(UnifiedEvent).where(UnifiedEvent.source_id == str(recovered.id)))
    assert projected.event_type == "OFFLINE_RECOGNITION_RECOVERED"
    assert projected.attention_required is False
    assert db.scalar(select(OperationalAlert)) is None


def test_health_transition_dedup_acknowledge_and_automatic_recovery(db: Session) -> None:
    site, camera = site_camera(db)
    actor = User(username="admin", full_name="Admin", password_hash="unused", is_active=True,
                 must_change_password=False, failed_login_count=0)
    db.add(actor); db.flush()
    service = ComponentHealthService(db)
    start = datetime.now(timezone.utc)
    service.observe(component_type="CAMERA", component_id=str(camera.id), display_name=camera.code,
        state="HEALTHY", summary="heartbeat current", site_id=site.id, observed_at=start)
    service.observe(component_type="CAMERA", component_id=str(camera.id), display_name=camera.code,
        state="FAULT", summary="heartbeat stale", site_id=site.id, observed_at=start + timedelta(seconds=60))
    service.observe(component_type="CAMERA", component_id=str(camera.id), display_name=camera.code,
        state="FAULT", summary="still stale", site_id=site.id, observed_at=start + timedelta(seconds=90))
    alerts = db.scalars(select(OperationalAlert)).all()
    assert len(alerts) == 1 and alerts[0].status == "ACTIVE" and alerts[0].occurrence_count == 2
    AlertService(db).acknowledge(alerts[0], actor_user_id=actor.id, note="Security is checking camera power")
    assert alerts[0].status == "ACKNOWLEDGED" and alerts[0].resolved_at is None
    service.observe(component_type="CAMERA", component_id=str(camera.id), display_name=camera.code,
        state="HEALTHY", summary="heartbeat restored", site_id=site.id, observed_at=start + timedelta(seconds=120))
    assert alerts[0].status == "RESOLVED" and alerts[0].resolved_at is not None
    assert db.scalars(select(UnifiedEvent).where(UnifiedEvent.event_type == "CAMERA_OFFLINE")).one()
    assert db.scalars(select(UnifiedEvent).where(UnifiedEvent.event_type == "CAMERA_ONLINE")).one()


def test_unverified_is_not_fault_and_ack_does_not_claim_physical_state(db: Session) -> None:
    service = ComponentHealthService(db)
    service.observe(component_type="PERIPHERAL", component_id="board:LED", display_name="LED display",
        state="UNVERIFIED", summary="Camera ACK only", capabilities={"claim": "UNVERIFIED", "camera_ack_only": True})
    health = db.scalar(select(ComponentHealth))
    assert health.health_state == "UNVERIFIED" and health.capabilities["camera_ack_only"] is True
    assert db.scalar(select(OperationalAlert)) is None


def test_recording_recovery_resolves_alert_and_retains_gap_event(db: Session) -> None:
    service = ComponentHealthService(db)
    start = datetime.now(timezone.utc)
    service.observe(component_type="RECORDING_SOURCE", component_id="source-1", display_name="Main Entry recording",
        state="HEALTHY", summary="Current", observed_at=start)
    service.observe(component_type="RECORDING_SOURCE", component_id="source-1", display_name="Main Entry recording",
        state="FAULT", summary="Stale", observed_at=start + timedelta(minutes=1))
    service.observe(component_type="RECORDING_SOURCE", component_id="source-1", display_name="Main Entry recording",
        state="HEALTHY", summary="Recording resumed", observed_at=start + timedelta(minutes=2))
    alert = db.scalar(select(OperationalAlert).where(OperationalAlert.alert_type == "RECORDING_STALE"))
    gap = db.scalar(select(UnifiedEvent).where(UnifiedEvent.event_type == "RECORDING_GAP"))
    assert alert.status == "RESOLVED"
    assert gap is not None and "gap from" in gap.summary


def test_monitor_is_observational_and_does_not_use_recognition_silence_as_camera_fault(db: Session) -> None:
    _, camera = site_camera(db)
    result = MonitoringService(db).run_once(include_network_probe=False)
    observed = db.scalar(select(ComponentHealth).where(ComponentHealth.component_type == "CAMERA", ComponentHealth.component_id == str(camera.id)))
    assert result["components_observed"] >= 6
    assert observed.health_state == "HEALTHY"
    assert db.scalar(select(OperationalAlert).where(OperationalAlert.component_type == "CAMERA")) is None


def test_monitoring_pages_permissions_csrf_navigation_and_bounded_api(client: TestClient, db: Session) -> None:
    assert client.get("/events").status_code == 303
    user(db, ("monitoring.view", "alerts.acknowledge")); site, camera = site_camera(db)
    now = datetime.now(timezone.utc)
    event = EventService(db).record(event_type="BLACKLIST_MATCH", occurred_at=now,
        source_type="test", source_id="one", display_subject="ABC1234", summary="Blacklist match",
        site_id=site.id, camera_id=camera.id)
    alert = AlertService(db).observe_condition(alert_type="CAMERA_OFFLINE", severity="WARNING",
        site_id=site.id, component_type="CAMERA", component_id=str(camera.id), title="Camera offline",
        summary="Heartbeat stale", observed_at=now)
    db.commit(); login(client)
    page = client.get("/events")
    assert page.status_code == 200 and "Event Center" in page.text and "ABC1234" in page.text
    assert 'data-nav-key="events"' in page.text and 'data-nav-key="system-health"' in page.text
    assert client.get(f"/events/{event.id}").status_code == 200
    health_page = client.get("/system-health")
    assert health_page.status_code == 200 and "Operational sites" in health_page.text
    detail = client.get(f"/alerts/{alert.id}")
    assert detail.status_code == 200 and "Acknowledging confirms human review only" in detail.text
    assert client.post(f"/alerts/{alert.id}/acknowledge", data={"note": "Investigating"}).status_code == 403
    response = client.post(f"/alerts/{alert.id}/acknowledge", data={"csrf_token": csrf(detail.text), "note": "Investigating camera power"})
    assert response.status_code == 303
    db.refresh(alert); assert alert.status == "ACKNOWLEDGED"
    api = client.get("/api/monitoring/events", params={"after": (now - timedelta(seconds=1)).isoformat()})
    assert api.status_code == 200 and api.json()["count"] >= 1 and len(api.json()["items"]) <= 50


def test_monitoring_view_does_not_grant_acknowledgement(client: TestClient, db: Session) -> None:
    user(db, ("monitoring.view",)); site, camera = site_camera(db)
    alert = AlertService(db).observe_condition(alert_type="CAMERA_OFFLINE", severity="WARNING",
        site_id=site.id, component_type="CAMERA", component_id=str(camera.id), title="Camera offline",
        summary="Heartbeat stale", observed_at=datetime.now(timezone.utc))
    db.commit(); login(client)
    detail = client.get(f"/alerts/{alert.id}")
    assert "Acknowledge Alert" not in detail.text
    assert client.post(f"/alerts/{alert.id}/acknowledge", data={"csrf_token": csrf(detail.text), "note": "Attempt"}).status_code == 403


def test_high_volume_all_events_is_bounded_and_attention_stays_concise(db: Session) -> None:
    site, camera = site_camera(db)
    service = EventService(db)
    started = datetime.now(timezone.utc)
    for index in range(120):
        service.record(event_type="ANPR_RECOGNITION", occurred_at=started + timedelta(milliseconds=index),
            source_type="LoadTestRecognition", source_id=str(index), display_subject=f"TEST{index:04d}",
            summary="Normal recognition", site_id=site.id, camera_id=camera.id)
    db.commit()
    query = MonitoringQueryService(db)
    assert query.events(tab="attention").total == 0
    first_page = query.events(tab="all")
    assert first_page.total == 120 and len(first_page.items) == 50 and first_page.page_count == 3
