from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models.access_list_entry import AccessListEntry
from app.models.camera import Camera
from app.models.parking import AccessDecision, AccessProfile, ParkingSession
from app.models.recognition_event import RecognitionEvent
from app.models.residential import ResidentialUnit, ResidentUnitMembership, VisitorReservation
from app.models.site import Site
from app.models.user import User
from app.services.access_list_service import AccessListService
from app.services.residential_identity_service import ResidentialIdentityService, ResidentialValidationError
from app.services.visitor_lifecycle_service import VisitorLifecycleService
from app.services.visitor_reservation_service import VisitorAuthorizationConflict, VisitorReservationService, VisitorValidationError


def seed(db, code="MAIN"):
    site = Site(code=code, name=f"{code} Site", timezone="Asia/Kuala_Lumpur", is_active=True)
    user = User(username=f"admin-{code.lower()}", full_name="Administrator", password_hash="hash", is_active=True, must_change_password=False, failed_login_count=0)
    db.add_all((site, user)); db.flush()
    profile = AccessProfile(site_id=site.id, name="Visitor Access", enabled=True)
    db.add(profile); db.flush()
    unit = ResidentialIdentityService(db).create_unit(site_id=site.id, unit_code="A-12-03")
    return site, user, profile, unit


def reservation(db, site, user, profile, unit, *, plate="ABC1234", start=None, mode="SINGLE_VISIT"):
    now = start or datetime.now(timezone.utc)
    return VisitorReservationService(db).create(
        site_id=site.id, unit_id=unit.id, visitor_name="Alice Wong", visitor_phone=None,
        purpose="Visit", plate=plate, access_profile_id=profile.id, max_stay_minutes_override=90,
        valid_from=now - timedelta(minutes=5), valid_until=now + timedelta(hours=4),
        expected_arrival_at=now, visit_mode=mode, host_resident_id=None, requested_by_user_id=user.id,
    )


def test_unit_uniqueness_is_site_scoped(db):
    site, _, _, _ = seed(db)
    service = ResidentialIdentityService(db)
    with pytest.raises(ResidentialValidationError):
        service.create_unit(site_id=site.id, unit_code=" a-12-03 ")
    other, _, _, other_unit = seed(db, "WEST")
    assert other_unit.unit_code_normalized == "A-12-03" and other_unit.site_id == other.id


def test_resident_membership_is_historical_and_not_authorization(db):
    site, _, _, unit = seed(db)
    service = ResidentialIdentityService(db)
    resident = service.create_resident(display_name="John Tan")
    membership = service.add_membership(resident_id=resident.id, unit_id=unit.id, membership_type="OWNER", primary=True)
    assert membership.active and db.scalar(select(func.count()).select_from(AccessListEntry)) == 0
    assert service.end_membership(membership)
    assert not membership.active and membership.valid_until is not None


def test_vehicle_identity_link_preserves_blacklist(db):
    site, _, _, unit = seed(db)
    entry = AccessListService(db).create(plate="BAD123", site_id=site.id, list_type="BLOCK", enabled=True,
        daily_window_enabled=False, camera_comment="", access_profile_id=None, max_stay_minutes_override=None)
    link = ResidentialIdentityService(db).link_vehicle(unit_id=unit.id, access_entry_id=entry.id)
    assert link.active and entry.list_type == "BLOCK" and entry.enabled


def test_deactivation_requires_explicit_access_disable(db):
    site, _, _, unit = seed(db)
    entry = AccessListService(db).create(plate="RES123", site_id=site.id, list_type="ALLOW", enabled=True,
        daily_window_enabled=False, camera_comment="", access_profile_id=None, max_stay_minutes_override=None)
    ResidentialIdentityService(db).link_vehicle(unit_id=unit.id, access_entry_id=entry.id)
    service = ResidentialIdentityService(db)
    assert service.set_unit_active(unit, False, disable_access=False) == () and entry.enabled
    changed = service.set_unit_active(unit, False, disable_access=True)
    assert changed == (entry.id,) and not entry.enabled


def test_no_plate_approval_creates_no_access(db):
    site, user, profile, unit = seed(db)
    item = reservation(db, site, user, profile, unit, plate="")
    approved, changed = VisitorReservationService(db).approve(item.id, actor_user_id=user.id)
    assert approved.approval_status == "APPROVED" and approved.access_binding_mode == "NONE"
    assert changed is None and db.scalar(select(func.count()).select_from(AccessListEntry)) == 0


def test_new_plate_approval_creates_temporary_m13_access_idempotently(db):
    site, user, profile, unit = seed(db)
    item = reservation(db, site, user, profile, unit)
    approved, entry_id = VisitorReservationService(db).approve(item.id, actor_user_id=user.id)
    entry = db.get(AccessListEntry, entry_id)
    assert approved.access_binding_mode == "TEMPORARY_ACCESS"
    assert entry.list_type == "ALLOW" and entry.enabled and entry.access_profile_id == profile.id
    assert entry.vehicle.reference_name == "Alice Wong" and entry.max_stay_minutes_override == 90
    _, second = VisitorReservationService(db).approve(item.id, actor_user_id=user.id)
    assert second is None and db.scalar(select(func.count()).select_from(AccessListEntry)) == 1


def test_existing_whitelist_is_reused_without_mutation(db):
    site, user, profile, unit = seed(db)
    old_profile = AccessProfile(site_id=site.id, name="Resident", enabled=True); db.add(old_profile); db.flush()
    entry = AccessListService(db).create(plate="ABC1234", site_id=site.id, list_type="ALLOW", enabled=True,
        daily_window_enabled=False, camera_comment="HOME", access_profile_id=old_profile.id, max_stay_minutes_override=None)
    item = reservation(db, site, user, profile, unit)
    VisitorReservationService(db).approve(item.id, actor_user_id=user.id)
    assert item.access_binding_mode == "EXISTING_ACCESS" and item.access_list_entry_id == entry.id
    assert entry.access_profile_id == old_profile.id and entry.valid_until is None and entry.camera_comment == "HOME"


def test_blacklist_and_disabled_existing_records_are_conflicts(db):
    site, user, profile, unit = seed(db)
    entry = AccessListService(db).create(plate="ABC1234", site_id=site.id, list_type="BLOCK", enabled=True,
        daily_window_enabled=False, camera_comment="", access_profile_id=None, max_stay_minutes_override=None)
    item = reservation(db, site, user, profile, unit)
    with pytest.raises(VisitorAuthorizationConflict): VisitorReservationService(db).approve(item.id, actor_user_id=user.id)
    assert item.approval_status == "PENDING" and entry.list_type == "BLOCK"


def test_overlapping_temporary_visits_conflict(db):
    site, user, profile, unit = seed(db); service = VisitorReservationService(db)
    first = reservation(db, site, user, profile, unit); service.approve(first.id, actor_user_id=user.id)
    second = reservation(db, site, user, profile, unit)
    # Existing temporary access is never silently repurposed.
    with pytest.raises(VisitorAuthorizationConflict): service.approve(second.id, actor_user_id=user.id)


def test_non_overlapping_managed_temporary_visit_reuses_central_record(db):
    site, user, profile, unit = seed(db); service = VisitorReservationService(db)
    now = datetime.now(timezone.utc)
    first = reservation(db, site, user, profile, unit, start=now); service.approve(first.id, actor_user_id=user.id)
    service.cancel(first.id, actor_user_id=user.id, reason="Completed commissioning visit")
    second = reservation(db, site, user, profile, unit, start=now + timedelta(days=1))
    _, changed = service.approve(second.id, actor_user_id=user.id)
    assert changed == first.access_list_entry_id == second.access_list_entry_id
    assert second.access_binding_mode == "TEMPORARY_ACCESS"
    assert db.scalar(select(func.count()).select_from(AccessListEntry)) == 1


def test_cancel_disables_only_managed_temporary_access(db):
    site, user, profile, unit = seed(db); service = VisitorReservationService(db)
    item = reservation(db, site, user, profile, unit); service.approve(item.id, actor_user_id=user.id)
    entry = db.get(AccessListEntry, item.access_list_entry_id)
    _, changed = service.cancel(item.id, actor_user_id=user.id, reason="Visit cancelled")
    assert changed == entry.id and not entry.enabled and item.approval_status == "CANCELLED"


def test_assign_plate_after_approval_creates_access_without_decision_or_session(db):
    site, user, profile, unit = seed(db); service = VisitorReservationService(db)
    item = reservation(db, site, user, profile, unit, plate=""); service.approve(item.id, actor_user_id=user.id)
    _, entry_id = service.assign_plate(item.id, plate="abc999")
    assert entry_id and item.plate_normalized == "ABC999"
    assert db.scalar(select(AccessDecision)) is None and db.scalar(select(ParkingSession)) is None


def test_live_single_visit_lifecycle_links_session_and_consumes_temporary_access(db):
    site, user, profile, unit = seed(db); now = datetime.now(timezone.utc)
    visitor = reservation(db, site, user, profile, unit, start=now); VisitorReservationService(db).approve(visitor.id, actor_user_id=user.id)
    entry = db.get(AccessListEntry, visitor.access_list_entry_id)
    entry_camera = Camera(site_id=site.id, code="ENTRY", name="Entry", direction="ENTRY", lane_role="ENTRY", is_enabled=True)
    exit_camera = Camera(site_id=site.id, code="EXIT", name="Exit", direction="EXIT", lane_role="EXIT", is_enabled=True)
    db.add_all((entry_camera, exit_camera)); db.flush()
    enter = RecognitionEvent(camera_id=entry_camera.id, site_id=site.id, plate_text="ABC1234", source="LIVE", received_at=now, captured_at=now, event_type="FULL")
    db.add(enter); db.flush()
    decision = AccessDecision(recognition_event_id=enter.id, site_id=site.id, camera_id=entry_camera.id, vehicle_id=entry.vehicle_id, access_entry_id=entry.id, access_status="WHITELIST_ACTIVE", decision="ALLOW", reason_code="ACTIVE_WHITELIST", policy_type="RESIDENTIAL", policy_version=1, evaluated_at=now, response_status="NOT_CONFIGURED", barrier_intent="OPEN", physical_execution="NOT_IMPLEMENTED")
    session = ParkingSession(site_id=site.id, vehicle_id=entry.vehicle_id, access_profile_id=profile.id, entry_recognition_id=enter.id, status="OPEN", entered_at=now, source="LIVE")
    db.add_all((decision, session)); db.flush()
    assert VisitorLifecycleService(db).process(enter.id) is None
    assert session.visitor_reservation_id == visitor.id and visitor.visit_state == "ARRIVED"
    leave_at = now + timedelta(hours=1)
    leave = RecognitionEvent(camera_id=exit_camera.id, site_id=site.id, plate_text="ABC1234", source="LIVE", received_at=leave_at, captured_at=leave_at, event_type="FULL")
    db.add(leave); db.flush()
    db.add(AccessDecision(recognition_event_id=leave.id, site_id=site.id, camera_id=exit_camera.id, vehicle_id=entry.vehicle_id, access_entry_id=entry.id, access_status="WHITELIST_ACTIVE", decision="ALLOW", reason_code="FREE_EXIT", policy_type="RESIDENTIAL", policy_version=1, evaluated_at=leave_at, response_status="NOT_CONFIGURED", barrier_intent="OPEN", physical_execution="NOT_IMPLEMENTED"))
    session.exit_recognition_id = leave.id; session.exited_at = leave_at; session.status = "CLOSED"; db.flush()
    assert VisitorLifecycleService(db).process(leave.id) == entry.id
    assert visitor.visit_state == "COMPLETED" and not entry.enabled


def test_offline_events_never_change_visitor_lifecycle(db):
    site, user, profile, unit = seed(db); item = reservation(db, site, user, profile, unit)
    VisitorReservationService(db).approve(item.id, actor_user_id=user.id)
    camera = Camera(site_id=site.id, code="ENTRY", name="Entry", direction="ENTRY", lane_role="ENTRY", is_enabled=True); db.add(camera); db.flush()
    now = datetime.now(timezone.utc); event = RecognitionEvent(camera_id=camera.id, site_id=site.id, plate_text="ABC1234", source="OFFLINE_RECOVERY", received_at=now, captured_at=now, event_type="FULL")
    db.add(event); db.flush()
    assert VisitorLifecycleService(db).process(event.id) is None and item.visit_state == "EXPECTED"
