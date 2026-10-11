import re
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password
from app.db.dependencies import get_db
from app.main import app
from app.models.parking import AccessProfile, DecisionResponseProfile, SitePolicy
from app.models.permission import Permission
from app.models.role import Role
from app.models.site import Site
from app.models.user import User
from app.models.camera import Camera
from app.models.parking import AccessDecision, ParkingSession


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


def create_user(db, permissions):
    role = Role(code="administrator", name="Administrator", is_system=True)
    role.permissions = [Permission(code=code, description=code) for code in permissions]
    user = User(username="admin", full_name="Administrator", password_hash=hash_password(PASSWORD), is_active=True, must_change_password=False, failed_login_count=0, roles=[role])
    db.add(user); db.commit()


def csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client):
    page = client.get("/login")
    assert client.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": csrf(page.text), "next": "/dashboard"}).status_code == 303


def seed(db):
    site = Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur", is_active=True)
    db.add(site); db.flush()
    profiles = []
    for kind in ("ALLOW", "DENY", "REVIEW"):
        profile = DecisionResponseProfile(site_id=site.id, name=kind.title(), response_kind=kind, led_rows=[], display_seconds=5, traffic_state="GREEN" if kind == "ALLOW" else "RED", traffic_green_seconds=5, speaker_enabled=False, speaker_message="{plate}", speaker_volume=7, speaker_speed=5, speaker_voice=3, speaker_pitch=5, plate_pause_ms=100, enabled=True)
        db.add(profile); profiles.append(profile)
    db.flush()
    db.add(SitePolicy(site_id=site.id, operating_mode="RESIDENTIAL", unknown_entry_policy="DENY", exit_mode="FREE_EXIT", exit_blacklist_policy="DENY", allow_response_profile_id=profiles[0].id, deny_response_profile_id=profiles[1].id, review_response_profile_id=profiles[2].id, policy_version=1))
    db.commit(); return site


def test_parking_pages_require_permissions(client, db):
    assert client.get("/parking/on-site").status_code == 303
    create_user(db, ())
    login(client)
    assert client.get("/parking/on-site").status_code == 403
    assert client.get("/parking/policy").status_code == 403


def test_parking_pages_render_and_profile_mutation_requires_csrf(client, db):
    create_user(db, ("parking.view", "parking.manage")); site = seed(db); login(client)
    on_site = client.get("/parking/on-site")
    assert on_site.status_code == 200 and "On-Site Vehicles" in on_site.text
    policy = client.get("/parking/policy")
    assert policy.status_code == 200 and "Residential Decision Rules" in policy.text
    assert client.post("/access-profiles", data={"site_id": str(site.id), "name": "Visitor", "enabled": "true"}).status_code == 403
    response = client.post("/access-profiles", data={"csrf_token": csrf(policy.text), "site_id": str(site.id), "name": "Visitor", "description": "Short stay", "default_max_stay_minutes": "120", "overstay_monitoring_enabled": "true", "enabled": "true", "normal_exit_policy": "ALLOW", "expired_exit_policy": "REVIEW", "overstay_exit_policy": "DENY", "blacklist_exit_policy": "DENY", "overstay_exit_grace_minutes": "15"})
    assert response.status_code == 303
    profile = db.scalar(select(AccessProfile).where(AccessProfile.name == "Visitor"))
    assert profile is not None and profile.default_max_stay_minutes == 120
    assert (profile.normal_exit_policy, profile.overstay_exit_policy, profile.overstay_exit_grace_minutes) == ("ALLOW", "DENY", 15)
    reviews = client.get("/parking/reviews")
    assert reviews.status_code == 200 and "Exit Reviews" in reviews.text


def test_policy_simulator_has_no_operational_side_effects(client, db):
    create_user(db, ("parking.manage",)); site = seed(db)
    camera = Camera(site_id=site.id, code="MAIN-ENTRY", name="Entry", direction="ENTRY", lane_role="ENTRY", is_enabled=True)
    db.add(camera); db.commit(); login(client)
    page = client.get(f"/parking/policy?site={site.id}")
    response = client.post("/parking/policy/simulate", data={"csrf_token": csrf(page.text), "site_id": str(site.id), "camera_id": str(camera.id), "plate": "UNKNOWN1", "evaluation_time": "2026-10-08T12:00"})
    assert response.status_code == 200
    assert "Denied" in response.text and "Not Listed" in response.text
    assert db.scalar(select(AccessDecision)) is None
    assert db.scalar(select(ParkingSession)) is None


def test_response_profile_standard_sources_are_dropdown_backed_and_validated(client, db):
    create_user(db, ("parking.manage",)); seed(db); login(client)
    profile = db.scalar(select(DecisionResponseProfile).where(DecisionResponseProfile.response_kind == "ALLOW"))
    page = client.get(f"/parking/response-profiles/{profile.id}")
    assert page.status_code == 200
    assert '<select name="row_1_value"' in page.text
    for source in ("PLATE", "ACCESS_STATUS", "CAMERA_NAME", "SITE_NAME", "CURRENT_DATE", "CURRENT_TIME", "CURRENT_WEEKDAY", "CURRENT_WEEK_NUMBER", "CURRENT_DATETIME"):
        assert f'value="{source}"' in page.text
    assert 'name="led_brightness"' in page.text and 'name="led_scroll_speed"' in page.text
    response = client.post(f"/parking/response-profiles/{profile.id}", data={
        "csrf_token": csrf(page.text), "name": "Allow", "display_seconds": "5",
        "traffic_state": "GREEN", "traffic_green_seconds": "5", "row_1_type": "STANDARD",
        "row_1_value": "UNSUPPORTED_FIELD", "row_2_type": "CUSTOM", "row_2_value": "Welcome",
        "row_3_type": "CUSTOM", "row_3_value": "", "row_4_type": "CUSTOM", "row_4_value": "",
    })
    assert response.status_code == 400


def test_response_profile_editor_conditions_irrelevant_settings_and_normalizes_blank_rows(client, db):
    create_user(db, ("parking.manage",)); seed(db); login(client)
    profile = db.scalar(select(DecisionResponseProfile).where(DecisionResponseProfile.response_kind == "DENY"))
    page = client.get(f"/parking/response-profiles/{profile.id}")
    assert page.status_code == 200
    for text in ("General", "Traffic Indicator", "LED Display", "Speaker", "Live preview", "Physical barrier"):
        assert text in page.text
    for preview_setting in ("--led-opacity:", "--led-glow:", "--scroll-duration:"):
        assert preview_setting in page.text
    assert "Long text scrolls continuously from right to left" in page.text
    assert 'type="radio" name="traffic_state"' in page.text
    response = client.post(f"/parking/response-profiles/{profile.id}", data={
        "csrf_token": csrf(page.text), "name": "Access Denied", "display_seconds": "10",
        "traffic_state": "RED", "traffic_green_seconds": "999", "led_brightness": "3",
        "led_scroll_speed": "2", "row_1_type": "STANDARD",
        "row_1_value": "PLATE", "row_2_type": "CUSTOM", "row_2_value": "",
        "row_3_type": "CUSTOM", "row_3_value": "CONTACT SECURITY", "row_4_type": "STANDARD",
        "row_4_value": "BLANK",
    })
    assert response.status_code == 303
    db.refresh(profile)
    assert profile.traffic_green_seconds == 5
    assert profile.speaker_enabled is False and profile.speaker_message == "{plate}"
    assert profile.led_brightness == 3 and profile.led_scroll_speed == 2
    assert profile.led_rows[1] == {"type": "STANDARD", "source": "BLANK"}

    invalid_green = client.post(f"/parking/response-profiles/{profile.id}", data={
        "csrf_token": csrf(page.text), "name": "Access Denied", "display_seconds": "10",
        "traffic_state": "GREEN", "traffic_green_seconds": "999", "row_1_type": "STANDARD",
        "row_1_value": "PLATE", "row_2_type": "STANDARD", "row_2_value": "BLANK",
        "row_3_type": "STANDARD", "row_3_value": "BLANK", "row_4_type": "STANDARD",
        "row_4_value": "BLANK",
    })
    assert invalid_green.status_code == 400

    invalid_led = client.post(f"/parking/response-profiles/{profile.id}", data={
        "csrf_token": csrf(page.text), "name": "Access Denied", "display_seconds": "10",
        "traffic_state": "RED", "led_brightness": "6", "led_scroll_speed": "0",
        "row_1_type": "STANDARD", "row_1_value": "CURRENT_DATETIME",
        "row_2_type": "STANDARD", "row_2_value": "BLANK", "row_3_type": "STANDARD",
        "row_3_value": "BLANK", "row_4_type": "STANDARD", "row_4_value": "BLANK",
    })
    assert invalid_led.status_code == 400


def test_access_profile_editor_uses_friendly_stay_units_and_policy_matrix(client, db):
    create_user(db, ("parking.manage",)); site = seed(db); login(client)
    page = client.get("/access-profiles")
    assert page.status_code == 200
    for text in ("Basic Profile", "Stay Rules", "Exit Enforcement", "Effective behavior", "Exit after access expiry"):
        assert text in page.text
    assert 'name="stay_limit_mode"' in page.text
    assert 'name="maximum_stay_unit"' in page.text
    response = client.post("/access-profiles", data={
        "csrf_token": csrf(page.text), "site_id": str(site.id), "name": "Four Hour Visitor",
        "stay_limit_mode": "LIMITED", "maximum_stay_value": "4", "maximum_stay_unit": "HOURS",
        "overstay_monitoring_enabled": "true", "enabled": "true", "normal_exit_policy": "ALLOW",
        "expired_exit_policy": "REVIEW", "overstay_exit_policy": "REVIEW", "blacklist_exit_policy": "DENY",
        "overstay_exit_grace_minutes": "15",
    })
    assert response.status_code == 303
    profile = db.scalar(select(AccessProfile).where(AccessProfile.name == "Four Hour Visitor"))
    assert profile.default_max_stay_minutes == 240 and profile.overstay_monitoring_enabled is True


def test_unlimited_profile_explicitly_disables_overstay_monitoring(client, db):
    create_user(db, ("parking.manage",)); site = seed(db); login(client)
    page = client.get("/access-profiles")
    response = client.post("/access-profiles", data={
        "csrf_token": csrf(page.text), "site_id": str(site.id), "name": "Resident Unlimited",
        "stay_limit_mode": "UNLIMITED", "maximum_stay_value": "99", "maximum_stay_unit": "DAYS",
        "overstay_monitoring_enabled": "true", "enabled": "true",
    })
    assert response.status_code == 303
    profile = db.scalar(select(AccessProfile).where(AccessProfile.name == "Resident Unlimited"))
    assert profile.default_max_stay_minutes is None and profile.overstay_monitoring_enabled is False
