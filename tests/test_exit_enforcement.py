from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.access_list_entry import AccessListEntry
from app.models.camera import Camera
from app.models.parking import AccessProfile, ParkingReviewItem, ParkingSession, SitePolicy
from app.models.recognition_event import RecognitionEvent
from app.models.site import Site
from app.models.vehicle import Vehicle
from app.services.parking_policy_service import DecisionContextBuilder, ResidentialPolicy, create_access_decision
from app.services.parking_review_service import ParkingReviewService
from app.services.parking_session_service import ParkingSessionService


NOW = datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc)


def seeded(db, *, exit_rule="ALLOW", overstay_rule="REVIEW", blacklist_rule="DENY", grace=0, valid_until=None):
    site = Site(code="MAIN", name="Main", timezone="Asia/Kuala_Lumpur", is_active=True)
    db.add(site); db.flush()
    entry_camera = Camera(site_id=site.id, code="ENTRY", name="Entry", direction="ENTRY", lane_role="ENTRY", is_enabled=True)
    exit_camera = Camera(site_id=site.id, code="EXIT", name="Exit", direction="EXIT", lane_role="EXIT", is_enabled=True)
    vehicle = Vehicle(plate_text="PFQ5217")
    profile = AccessProfile(site_id=site.id, name="Contractor", default_max_stay_minutes=60,
        overstay_monitoring_enabled=True, enabled=True, normal_exit_policy=exit_rule,
        expired_exit_policy="REVIEW", overstay_exit_policy=overstay_rule,
        blacklist_exit_policy=blacklist_rule, overstay_exit_grace_minutes=grace)
    db.add_all([entry_camera, exit_camera, vehicle, profile]); db.flush()
    access = AccessListEntry(vehicle_id=vehicle.id, site_id=site.id, list_type="ALLOW", enabled=True,
        valid_until=valid_until, daily_window_enabled=False, access_profile_id=profile.id)
    policy = SitePolicy(site_id=site.id, operating_mode="RESIDENTIAL", unknown_entry_policy="DENY",
        exit_mode="FREE_EXIT", exit_blacklist_policy="DENY", policy_version=1)
    db.add_all([access, policy]); db.flush()
    return site, entry_camera, exit_camera, vehicle, profile, access


def event_context(db, camera, site, *, at=NOW, source="LIVE"):
    event = RecognitionEvent(camera_id=camera.id, site_id=site.id, plate_text="PFQ5217", source=source,
        received_at=at, captured_at=at, event_type="FULL")
    db.add(event); db.flush()
    return DecisionContextBuilder(db).build(event)


def enter(db, site, camera):
    context = event_context(db, camera, site, at=NOW)
    return ParkingSessionService(db).apply(context, "ALLOW")


def test_active_session_uses_entry_time_snapshot_after_profile_edit(db):
    site, entry_camera, exit_camera, _vehicle, profile, _access = seeded(db, exit_rule="REVIEW")
    session = enter(db, site, entry_camera)
    assert session.effective_normal_exit_policy == "REVIEW"
    profile.normal_exit_policy = "ALLOW"
    result = ResidentialPolicy.evaluate(event_context(db, exit_camera, site, at=NOW + timedelta(minutes=10)))
    assert (result.decision, result.exit_policy_source) == ("REVIEW", "SESSION_PROFILE_SNAPSHOT")


def test_blacklist_precedes_overstay_and_maps_barrier_intent(db):
    site, entry_camera, exit_camera, _vehicle, _profile, access = seeded(db, overstay_rule="ALLOW", blacklist_rule="DENY")
    session = enter(db, site, entry_camera)
    session.overstay_due_at = NOW
    access.list_type = "BLOCK"
    result = ResidentialPolicy.evaluate(event_context(db, exit_camera, site, at=NOW + timedelta(hours=2)))
    assert result.reason_code == "PROFILE_BLACKLIST_EXIT_DENY"
    assert (result.decision, result.barrier_intent) == ("DENY", "HOLD")


def test_overstay_grace_boundary_and_unlimited_stay(db):
    site, entry_camera, exit_camera, _vehicle, _profile, _access = seeded(db, exit_rule="ALLOW", overstay_rule="REVIEW", grace=15)
    session = enter(db, site, entry_camera)
    session.overstay_due_at = NOW + timedelta(minutes=60)
    boundary = ResidentialPolicy.evaluate(event_context(db, exit_camera, site, at=NOW + timedelta(minutes=75)))
    after = ResidentialPolicy.evaluate(event_context(db, exit_camera, site, at=NOW + timedelta(minutes=75, seconds=1)))
    assert boundary.decision == "ALLOW"
    assert (after.decision, after.reason_code) == ("REVIEW", "PROFILE_OVERSTAY_EXIT_REVIEW")
    session.overstay_due_at = None
    unlimited = ResidentialPolicy.evaluate(event_context(db, exit_camera, site, at=NOW + timedelta(days=30)))
    assert unlimited.reason_code == "PROFILE_NORMAL_EXIT_ALLOW"


def test_review_attempts_coalesce_but_decisions_remain_distinct(db):
    site, entry_camera, exit_camera, _vehicle, _profile, _access = seeded(db, exit_rule="REVIEW")
    session = enter(db, site, entry_camera)
    service = ParkingReviewService(db)
    decision_ids = []
    for minute in (5, 6):
        context = event_context(db, exit_camera, site, at=NOW + timedelta(minutes=minute))
        decision = create_access_decision(db, context); decision_ids.append(decision.id)
        service.apply_exit_result(context, decision, session)
    items = db.scalars(select(ParkingReviewItem)).all()
    assert len(items) == 1 and items[0].attempt_count == 2
    assert len(set(decision_ids)) == 2


def test_allowed_exit_closes_session_and_resolves_review(db):
    site, entry_camera, exit_camera, _vehicle, profile, _access = seeded(db, exit_rule="REVIEW")
    session = enter(db, site, entry_camera)
    review_context = event_context(db, exit_camera, site, at=NOW + timedelta(minutes=5))
    review_decision = create_access_decision(db, review_context)
    ParkingReviewService(db).apply_exit_result(review_context, review_decision, session)
    session.effective_normal_exit_policy = "ALLOW"; profile.normal_exit_policy = "ALLOW"
    allow_context = event_context(db, exit_camera, site, at=NOW + timedelta(minutes=6))
    allow_decision = create_access_decision(db, allow_context)
    closed = ParkingSessionService(db).apply(allow_context, allow_decision.decision)
    ParkingReviewService(db).apply_exit_result(allow_context, allow_decision, closed)
    assert session.status == "CLOSED"
    assert db.scalar(select(ParkingReviewItem)).status == "RESOLVED"


def test_recovered_exit_has_no_intent_or_review(db):
    site, entry_camera, exit_camera, _vehicle, _profile, _access = seeded(db, exit_rule="DENY")
    enter(db, site, entry_camera)
    context = event_context(db, exit_camera, site, at=NOW + timedelta(minutes=5), source="OFFLINE_RECOVERY")
    decision = create_access_decision(db, context)
    ParkingReviewService(db).apply_exit_result(context, decision, context.active_session)
    assert (decision.decision, decision.barrier_intent) == ("NO_ACTION", "NONE")
    assert db.scalar(select(ParkingReviewItem)) is None
