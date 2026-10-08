from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select

from app.models.access_list_entry import AccessListEntry
from app.models.camera import Camera
from app.models.parking import AccessDecision, AccessProfile, OverstayAlert, ParkingSession, SitePolicy
from app.models.recognition_event import RecognitionEvent
from app.models.site import Site
from app.models.vehicle import Vehicle
from app.services.parking_policy_service import AccessStatusResolver, DecisionContextBuilder, ResidentialPolicy, create_access_decision
from app.services.parking_session_service import ParkingSessionService, scan_overstays


NOW = datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc)


def setup_context(db, *, lane="ENTRY", list_type="ALLOW", enabled=True, valid_from=None, valid_until=None):
    site = Site(code="MAIN", name="Main", timezone="Asia/Kuala_Lumpur", is_active=True)
    camera = Camera(site=site, code=f"CAM-{lane}", name=lane, direction=lane, lane_role=lane, is_enabled=True)
    vehicle = Vehicle(plate_text="WQT4120")
    entry = AccessListEntry(vehicle=vehicle, site=site, list_type=list_type, enabled=enabled,
        valid_from=valid_from, valid_until=valid_until, daily_window_enabled=False)
    policy = SitePolicy(site_id=site.id, operating_mode="RESIDENTIAL", unknown_entry_policy="DENY",
        exit_mode="FREE_EXIT", exit_blacklist_policy="DENY", policy_version=1)
    db.add_all([site, camera, vehicle, entry]); db.flush(); policy.site_id = site.id; db.add(policy)
    event = RecognitionEvent(camera_id=camera.id, site_id=site.id, plate_text=vehicle.plate_text,
        source="LIVE", received_at=NOW, captured_at=NOW, event_type="FULL")
    db.add(event); db.flush()
    return DecisionContextBuilder(db).build(event)


def test_access_status_resolver_covers_active_and_inactive_states(db):
    context = setup_context(db)
    assert context.access_status == "WHITELIST_ACTIVE"
    assert AccessStatusResolver.resolve(context.access_entry, at=NOW - timedelta(days=2), timezone_name="Asia/Kuala_Lumpur") == "WHITELIST_NOT_YET_VALID" if context.access_entry.valid_from else "WHITELIST_ACTIVE"
    context.access_entry.enabled = False
    assert AccessStatusResolver.resolve(context.access_entry, at=NOW, timezone_name="Asia/Kuala_Lumpur") == "WHITELIST_DISABLED"


def test_access_status_resolver_supports_overnight_window(db):
    context = setup_context(db)
    entry = context.access_entry; entry.daily_window_enabled = True; entry.daily_start = time(22); entry.daily_end = time(6)
    assert AccessStatusResolver.resolve(entry, at=NOW, timezone_name="UTC") == "WHITELIST_ACTIVE"
    assert AccessStatusResolver.resolve(entry, at=NOW.replace(hour=12), timezone_name="UTC") == "WHITELIST_OUTSIDE_WINDOW"


def test_residential_entry_and_free_exit_policy(db):
    entry_context = setup_context(db)
    assert ResidentialPolicy.evaluate(entry_context).decision == "ALLOW"
    entry_context.camera.lane_role = "EXIT"
    entry_context.access_entry.list_type = "BLOCK"
    exit_context = DecisionContextBuilder(db).build(entry_context.event)
    result = ResidentialPolicy.evaluate(exit_context)
    assert (result.decision, result.reason_code) == ("DENY", "EXIT_BLACKLIST_DENY")


def test_unspecified_lane_is_safe_no_action(db):
    context = setup_context(db, lane="UNSPECIFIED")
    assert ResidentialPolicy.evaluate(context).decision == "NO_ACTION"


def test_decision_is_persistent_and_idempotent(db):
    context = setup_context(db)
    first = create_access_decision(db, context)
    second = create_access_decision(db, context)
    assert first.id == second.id
    assert db.scalar(select(AccessDecision).where(AccessDecision.recognition_event_id == context.event.id)).decision == "ALLOW"


def test_entry_exit_session_and_overstay_lifecycle(db):
    context = setup_context(db)
    profile = AccessProfile(site_id=context.site.id, name="Visitor", default_max_stay_minutes=30, overstay_monitoring_enabled=True, enabled=True)
    db.add(profile); db.flush(); context.access_entry.access_profile_id = profile.id
    session = ParkingSessionService(db).apply(context, "ALLOW")
    assert session.status == "OPEN" and session.overstay_due_at == NOW + timedelta(minutes=30)
    assert scan_overstays(db, now=NOW + timedelta(minutes=31)) == 1
    assert session.status == "OVERSTAY"
    assert db.scalar(select(OverstayAlert).where(OverstayAlert.parking_session_id == session.id)) is not None

    exit_camera = Camera(site_id=context.site.id, code="CAM-EXIT", name="Exit", direction="EXIT", lane_role="EXIT", is_enabled=True)
    db.add(exit_camera); db.flush()
    exit_event = RecognitionEvent(camera_id=exit_camera.id, site_id=context.site.id, plate_text="WQT4120", source="LIVE", received_at=NOW + timedelta(hours=1), captured_at=NOW + timedelta(hours=1), event_type="FULL")
    db.add(exit_event); db.flush()
    exit_context = DecisionContextBuilder(db).build(exit_event)
    ParkingSessionService(db).apply(exit_context, "ALLOW")
    assert session.status == "CLOSED"
    assert db.scalar(select(OverstayAlert).where(OverstayAlert.parking_session_id == session.id)).status == "RESOLVED"


def test_offline_recovery_never_plans_physical_action(db):
    context = setup_context(db)
    context.event.source = "OFFLINE_RECOVERY"
    decision = create_access_decision(db, context)
    assert (decision.decision, decision.response_status) == ("NO_ACTION", "NOT_APPLICABLE")
