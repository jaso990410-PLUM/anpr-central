from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.access_list_entry import AccessListEntry
from app.models.barrier import BarrierAction
from app.models.camera import Camera
from app.models.operations import ParkingReconciliation, RecognitionPlateCorrection, VehicleWatch
from app.models.parking import AccessDecision, ParkingSession, ParkingSessionIssue, SitePolicy
from app.models.recognition_event import RecognitionEvent
from app.models.site import Site
from app.models.user import User
from app.models.vehicle import Vehicle
from app.services.operations_service import OperationsService
from app.services.parking_reconciliation_service import ParkingReconciliationService, ReconciliationConflict, ReconciliationError
from app.services.plate_correction_service import PlateCorrectionError, PlateCorrectionService, effective_plate


def seed_lane(db: Session):
    site = Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur", is_active=True)
    entry = Camera(site=site, code="MAIN-ENTRY", name="Main Entry", direction="ENTRY", lane_role="ENTRY", is_enabled=True)
    exit_camera = Camera(site=site, code="MAIN-EXIT", name="Main Exit", direction="EXIT", lane_role="EXIT", is_enabled=True)
    user = User(username="operator", full_name="Lane Operator", password_hash="hash", is_active=True, must_change_password=False, failed_login_count=0)
    db.add_all((site, entry, exit_camera, user)); db.flush()
    db.add(SitePolicy(site_id=site.id, operating_mode="RESIDENTIAL", unknown_entry_policy="REVIEW", exit_mode="FREE_EXIT", exit_blacklist_policy="DENY", policy_version=1))
    db.commit()
    return site, entry, exit_camera, user


def event(db: Session, camera: Camera, plate: str, at: datetime, *, source: str = "LIVE") -> RecognitionEvent:
    item = RecognitionEvent(camera_id=camera.id, site_id=camera.site_id, plate_text=plate, source=source, direction="0", confidence=99, captured_at=at, received_at=at, event_type="FULL")
    db.add(item); db.flush(); return item


def test_plate_corrections_preserve_raw_decision_and_append_history(db: Session) -> None:
    site, entry, _, user = seed_lane(db)
    recognized = event(db, entry, "WQT4I20", datetime.now(timezone.utc))
    decision = AccessDecision(recognition_event_id=recognized.id, site_id=site.id, camera_id=entry.id, access_status="NOT_LISTED", decision="REVIEW", reason_code="UNKNOWN", policy_type="RESIDENTIAL", policy_version=1, evaluated_at=recognized.captured_at, response_status="NOT_CONFIGURED", barrier_intent="NONE", physical_execution="NOT_IMPLEMENTED")
    db.add(decision); db.commit()
    service = PlateCorrectionService(db)

    first = service.correct(recognized.id, corrected_plate="wqt4120", reason="OCR character misread", user_id=user.id)
    second = service.correct(recognized.id, corrected_plate="WQT4720", reason="Operator reviewed scene", user_id=user.id)
    db.commit(); db.refresh(recognized); db.refresh(decision)

    assert recognized.plate_text == "WQT4I20"
    assert decision.decision == "REVIEW"
    assert second.supersedes_correction_id == first.id
    assert effective_plate(db, recognized) == "WQT4720"
    assert [item.id for item in service.history(recognized.id)] == [second.id, first.id]
    assert db.scalar(select(BarrierAction)) is None
    assert db.scalar(select(ParkingSession)) is None


def test_plate_correction_requires_reason_and_normalizes(db: Session) -> None:
    _, entry, _, user = seed_lane(db)
    recognized = event(db, entry, "ABC123", datetime.now(timezone.utc)); db.commit()
    with pytest.raises(PlateCorrectionError):
        PlateCorrectionService(db).correct(recognized.id, corrected_plate="abc124", reason="", user_id=user.id)
    correction = PlateCorrectionService(db).correct(recognized.id, corrected_plate="abc124", reason="Verified image", user_id=user.id)
    assert correction.corrected_plate_normalized == "ABC124"


def test_policy_preview_for_corrected_plate_is_side_effect_free(db: Session) -> None:
    site, entry, _, user = seed_lane(db)
    vehicle = Vehicle(plate_text="WQT4120")
    db.add(vehicle); db.flush(); db.add(AccessListEntry(vehicle_id=vehicle.id, site_id=site.id, list_type="ALLOW", enabled=True, daily_window_enabled=False))
    recognized = event(db, entry, "WQT4I20", datetime.now(timezone.utc)); db.commit()
    PlateCorrectionService(db).correct(recognized.id, corrected_plate="WQT4120", reason="Confirmed from plate crop", user_id=user.id); db.commit()

    detail = OperationsService(db).detail(recognized.id)

    assert detail["preview"]["decision"] == "Granted"
    assert db.scalar(select(AccessDecision)) is None
    assert db.scalar(select(ParkingSession)) is None
    assert db.scalar(select(BarrierAction)) is None


def test_link_exit_closes_session_resolves_issue_and_is_idempotent(db: Session) -> None:
    site, entry_camera, exit_camera, user = seed_lane(db)
    vehicle = Vehicle(plate_text="ABC1234"); db.add(vehicle); db.flush()
    entered = datetime.now(timezone.utc) - timedelta(hours=2)
    entry_event = event(db, entry_camera, vehicle.plate_text, entered)
    exit_event = event(db, exit_camera, vehicle.plate_text, entered + timedelta(hours=1))
    session = ParkingSession(site_id=site.id, vehicle_id=vehicle.id, entry_recognition_id=entry_event.id, status="OPEN", entered_at=entered, source="LIVE")
    issue = ParkingSessionIssue(site_id=site.id, vehicle_id=vehicle.id, recognition_event_id=exit_event.id, issue_type="EXIT_WITHOUT_ENTRY", status="OPEN")
    db.add_all((session, issue)); db.commit()
    service = ParkingReconciliationService(db)

    record, created = service.apply(action_type="LINK_EXIT_TO_OPEN_SESSION", action_token="link-one", primary_event_id=exit_event.id, session_id=session.id, user_id=user.id, reason="Exit OCR was corrected")
    duplicate, created_again = service.apply(action_type="LINK_EXIT_TO_OPEN_SESSION", action_token="link-one", primary_event_id=exit_event.id, session_id=session.id, user_id=user.id, reason="Exit OCR was corrected")
    db.commit(); db.refresh(session); db.refresh(issue)

    assert created and not created_again and duplicate.id == record.id
    assert session.status == "CLOSED" and session.exit_recognition_id == exit_event.id
    assert session.exited_at.replace(tzinfo=timezone.utc) == exit_event.captured_at
    assert issue.status == "RESOLVED"
    assert record.previous_state["status"] == "OPEN" and record.resulting_state["status"] == "CLOSED"
    assert db.scalar(select(BarrierAction)) is None


def test_reconciliation_rejects_impossible_or_stale_exit(db: Session) -> None:
    site, entry_camera, exit_camera, user = seed_lane(db)
    vehicle = Vehicle(plate_text="ABC1234"); db.add(vehicle); db.flush()
    entered = datetime.now(timezone.utc)
    entry_event = event(db, entry_camera, vehicle.plate_text, entered)
    early_exit = event(db, exit_camera, vehicle.plate_text, entered - timedelta(minutes=1))
    session = ParkingSession(site_id=site.id, vehicle_id=vehicle.id, entry_recognition_id=entry_event.id, status="OPEN", entered_at=entered, source="LIVE")
    db.add(session); db.commit()
    service = ParkingReconciliationService(db)
    with pytest.raises(ReconciliationError, match="earlier"):
        service.apply(action_type="LINK_EXIT_TO_OPEN_SESSION", action_token="early", primary_event_id=early_exit.id, session_id=session.id, user_id=user.id, reason="Testing chronology")
    session.status = "CLOSED"; session.exited_at = entered + timedelta(minutes=2); db.commit()
    with pytest.raises(ReconciliationConflict, match="already changed"):
        service.apply(action_type="LINK_EXIT_TO_OPEN_SESSION", action_token="stale", primary_event_id=early_exit.id, session_id=session.id, user_id=user.id, reason="Stale operator page")


def test_create_reconciled_sessions_duplicate_and_recovered_are_non_physical(db: Session) -> None:
    site, entry_camera, exit_camera, user = seed_lane(db)
    entered = datetime.now(timezone.utc) - timedelta(hours=3)
    recovered_entry = event(db, entry_camera, "REC123", entered, source="OFFLINE_RECOVERY")
    recovered_exit = event(db, exit_camera, "REC123", entered + timedelta(hours=2), source="OFFLINE_RECOVERY")
    db.commit(); service = ParkingReconciliationService(db)
    closed, _ = service.apply(action_type="CREATE_CLOSED_SESSION_FROM_ENTRY_EXIT_PAIR", action_token="closed-pair", primary_event_id=recovered_entry.id, secondary_event_id=recovered_exit.id, user_id=user.id, reason="Recovered historical pair")
    live_entry = event(db, entry_camera, "OPEN123", datetime.now(timezone.utc))
    opened, _ = service.apply(action_type="CREATE_OPEN_SESSION_FROM_ENTRY", action_token="open-one", primary_event_id=live_entry.id, user_id=user.id, reason="Manual release entry")
    duplicate_event = event(db, entry_camera, "OPEN123", datetime.now(timezone.utc) + timedelta(minutes=1))
    issue = ParkingSessionIssue(site_id=site.id, vehicle_id=opened.parking_session.vehicle_id, recognition_event_id=duplicate_event.id, issue_type="DUPLICATE_ENTRY", status="OPEN")
    db.add(issue); db.flush()
    duplicate, _ = service.apply(action_type="MARK_DUPLICATE_ENTRY", action_token="dup-one", primary_event_id=duplicate_event.id, session_id=opened.parking_session_id, user_id=user.id, reason="Same vehicle remained onsite")
    db.commit()
    assert closed.parking_session.status == "CLOSED" and closed.parking_session.source == "RECONCILED"
    assert opened.parking_session.status == "OPEN" and opened.parking_session.source == "RECONCILED"
    assert duplicate.parking_session_id == opened.parking_session_id
    assert db.scalar(select(BarrierAction)) is None
    assert db.scalar(select(AccessDecision)) is None


def test_resolve_without_match_does_not_fabricate_session(db: Session) -> None:
    site, _, exit_camera, user = seed_lane(db)
    exit_event = event(db, exit_camera, "NOMATCH", datetime.now(timezone.utc))
    issue = ParkingSessionIssue(site_id=site.id, recognition_event_id=exit_event.id, issue_type="EXIT_WITHOUT_ENTRY", status="OPEN")
    db.add(issue); db.commit()
    record, _ = ParkingReconciliationService(db).apply(action_type="RESOLVE_WITHOUT_MATCH", action_token="no-match", primary_event_id=exit_event.id, user_id=user.id, reason="Vehicle entered before Central started")
    db.commit(); db.refresh(issue)
    assert issue.status == "RESOLVED" and record.parking_session_id is None
    assert db.scalar(select(ParkingSession)) is None


def test_attach_recovered_event_records_history_without_replaying_actions(db: Session) -> None:
    site, entry_camera, _, user = seed_lane(db)
    vehicle = Vehicle(plate_text="RECOVER1"); db.add(vehicle); db.flush()
    entered = datetime.now(timezone.utc) - timedelta(hours=1)
    original_entry = event(db, entry_camera, vehicle.plate_text, entered)
    recovered = event(db, entry_camera, vehicle.plate_text, entered + timedelta(minutes=2), source="OFFLINE_RECOVERY")
    session = ParkingSession(site_id=site.id, vehicle_id=vehicle.id, entry_recognition_id=original_entry.id, status="OPEN", entered_at=entered, source="LIVE")
    db.add(session); db.commit()

    record, created = ParkingReconciliationService(db).apply(
        action_type="ATTACH_RECOVERED_EVENT_TO_SESSION",
        action_token="attach-recovered-entry",
        primary_event_id=recovered.id,
        session_id=session.id,
        user_id=user.id,
        reason="Recovered duplicate belongs to the existing stay",
    )
    db.commit(); db.refresh(session); db.refresh(recovered)

    assert created and record.parking_session_id == session.id
    assert session.status == "OPEN" and session.entry_recognition_id == original_entry.id
    assert recovered.source == "OFFLINE_RECOVERY"
    assert db.scalar(select(BarrierAction)) is None
    assert db.scalar(select(AccessDecision)) is None


def test_fuzzy_search_is_bounded_and_has_no_policy_side_effects(db: Session) -> None:
    site, entry, _, _ = seed_lane(db)
    for plate in ("PFQ5217", "PFQ5277", "ZZZ9999"):
        vehicle = Vehicle(plate_text=plate); db.add(vehicle); db.flush(); db.add(AccessListEntry(vehicle_id=vehicle.id, site_id=site.id, list_type="ALLOW", enabled=True, daily_window_enabled=False))
    event(db, entry, "PFQ5211", datetime.now(timezone.utc)); db.commit()
    results = OperationsService(db).similar_plates("pfq52i7", site_id=site.id)
    plates = [item.plate for item in results]
    assert plates[0] == "PFQ5217" and "PFQ5277" in plates and "ZZZ9999" not in plates
    assert db.scalar(select(AccessDecision)) is None and db.scalar(select(BarrierAction)) is None


def test_watch_history_is_site_scoped_and_independent_from_access(db: Session) -> None:
    site, entry, _, user = seed_lane(db)
    recognized = event(db, entry, "WATCH1", datetime.now(timezone.utc))
    decision = AccessDecision(recognition_event_id=recognized.id, site_id=site.id, camera_id=entry.id, access_status="WHITELIST_ACTIVE", decision="ALLOW", reason_code="ACTIVE_WHITELIST", policy_type="RESIDENTIAL", policy_version=1, evaluated_at=recognized.captured_at, response_status="NOT_CONFIGURED", barrier_intent="OPEN", physical_execution="NOT_IMPLEMENTED")
    db.add(decision); db.commit(); service = OperationsService(db)
    watch = service.add_watch(site_id=site.id, plate="watch1", severity="HIGH_ATTENTION", note="Security requested attention", user_id=user.id); db.commit()
    assert service.detail(recognized.id)["watch"].id == watch.id
    assert decision.decision == "ALLOW" and db.scalar(select(BarrierAction)) is None
    service.clear_watch(watch.id, user_id=user.id); db.commit(); db.refresh(watch)
    assert not watch.active and watch.cleared_at is not None
    assert db.get(VehicleWatch, watch.id) is not None
