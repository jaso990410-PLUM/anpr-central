from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.barrier import BarrierAction
from app.models.camera import Camera
from app.models.media import MediaSource, RecordingSegment
from app.models.monitoring import OperationalAlert, UnifiedEvent
from app.models.offline_recovery_run import OfflineRecoveryRun
from app.models.operations import ParkingReconciliation, RecognitionPlateCorrection
from app.models.parking import AccessDecision, ParkingSession
from app.models.recognition_event import RecognitionEvent
from app.models.site import Site
from app.models.user import User
from app.models.vehicle import Vehicle
from app.services.reporting_service import ReportingService


def seed_site(db: Session):
    site = Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur", is_active=True)
    entry = Camera(site=site, code="MAIN-ENTRY", name="Main Entry", direction="ENTRY", lane_role="ENTRY", is_enabled=True)
    exit_camera = Camera(site=site, code="MAIN-EXIT", name="Main Exit", direction="EXIT", lane_role="EXIT", is_enabled=True)
    vehicle = Vehicle(plate_text="ABC1234")
    user = User(username="operator", full_name="Operator", password_hash="hash", is_active=True,
                must_change_password=False, failed_login_count=0)
    db.add_all((site, entry, exit_camera, vehicle, user)); db.flush()
    return site, entry, exit_camera, vehicle, user


def recognition(db: Session, camera: Camera, at: datetime, *, source: str = "LIVE", plate: str = "ABC1234"):
    item = RecognitionEvent(camera_id=camera.id, site_id=camera.site_id, plate_text=plate,
                            source=source, captured_at=at, received_at=at, event_type="FULL")
    db.add(item); db.flush(); return item


def test_traffic_uses_sessions_and_reconstructs_boundary_occupancy(db: Session) -> None:
    site, entry, exit_camera, vehicle, _ = seed_site(db)
    # Local report day is 15 Oct; UTC is eight hours behind.
    before = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)  # 23:00 local
    closes = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)  # 02:00 local
    first_entry = recognition(db, entry, before)
    first_exit = recognition(db, exit_camera, closes)
    db.add(ParkingSession(site_id=site.id, vehicle_id=vehicle.id, entry_recognition_id=first_entry.id,
                          exit_recognition_id=first_exit.id, status="CLOSED", entered_at=before, exited_at=closes))
    vehicle_two = Vehicle(plate_text="XYZ9876"); db.add(vehicle_two); db.flush()
    inside = datetime(2026, 10, 15, 1, 0, tzinfo=timezone.utc)  # 09:00 local
    second_entry = recognition(db, entry, inside, plate=vehicle_two.plate_text)
    db.add(ParkingSession(site_id=site.id, vehicle_id=vehicle_two.id, entry_recognition_id=second_entry.id,
                          status="OPEN", entered_at=inside))
    # Recognition noise does not create another traffic movement.
    recognition(db, entry, inside + timedelta(seconds=10), plate=vehicle_two.plate_text)
    db.commit()

    service = ReportingService(db)
    query = service.query(site_id=site.id, period="custom", date_from="2026-10-15", date_to="2026-10-15",
                          now=datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc))
    data = service.traffic(query)

    assert data["entries"] == 1
    assert data["exits"] == 1
    assert data["peak_occupancy"] == 1
    assert data["recognition_live"] == 3
    assert data["average_stay_seconds"] == 3 * 3600
    assert data["occupancy"][0]["occupancy"] == 1


def test_lane_filter_uses_entry_and_exit_camera_roles_independently(db: Session) -> None:
    site, entry, exit_camera, vehicle, _ = seed_site(db)
    entered = datetime(2026, 10, 15, 1, tzinfo=timezone.utc)
    exited = entered + timedelta(hours=2)
    enter_event = recognition(db, entry, entered)
    exit_event = recognition(db, exit_camera, exited)
    db.add(ParkingSession(site_id=site.id, vehicle_id=vehicle.id, entry_recognition_id=enter_event.id,
                          exit_recognition_id=exit_event.id, status="CLOSED", entered_at=entered, exited_at=exited))
    db.commit(); service = ReportingService(db)
    entry_query = service.query(site_id=site.id, camera_id=entry.id, period="custom",
                                date_from="2026-10-15", date_to="2026-10-15")
    exit_query = service.query(site_id=site.id, camera_id=exit_camera.id, period="custom",
                               date_from="2026-10-15", date_to="2026-10-15")
    assert (service.traffic(entry_query)["entries"], service.traffic(entry_query)["exits"]) == (1, 0)
    assert (service.traffic(exit_query)["entries"], service.traffic(exit_query)["exits"]) == (0, 1)


def test_simultaneous_exit_and_entry_do_not_create_false_occupancy_peak(db: Session) -> None:
    site, entry, exit_camera, vehicle, _ = seed_site(db)
    start = datetime(2026, 10, 15, 0, tzinfo=timezone.utc)
    handover = start + timedelta(hours=1)
    first_entry = recognition(db, entry, start)
    first_exit = recognition(db, exit_camera, handover)
    db.add(ParkingSession(site_id=site.id, vehicle_id=vehicle.id, entry_recognition_id=first_entry.id,
                          exit_recognition_id=first_exit.id, status="CLOSED", entered_at=start, exited_at=handover))
    next_vehicle = Vehicle(plate_text="NEXT123"); db.add(next_vehicle); db.flush()
    next_entry = recognition(db, entry, handover, plate=next_vehicle.plate_text)
    db.add(ParkingSession(site_id=site.id, vehicle_id=next_vehicle.id, entry_recognition_id=next_entry.id,
                          status="OPEN", entered_at=handover))
    db.commit()
    query = ReportingService(db).query(site_id=site.id, period="custom",
                                       date_from="2026-10-15", date_to="2026-10-15")
    assert ReportingService(db).traffic(query)["peak_occupancy"] == 1


def test_access_operations_and_recovery_are_aggregated_without_mutation(db: Session) -> None:
    site, entry, _, vehicle, user = seed_site(db)
    now = datetime(2026, 10, 15, 1, tzinfo=timezone.utc)
    event = recognition(db, entry, now)
    decision = AccessDecision(recognition_event_id=event.id, site_id=site.id, camera_id=entry.id,
                              vehicle_id=vehicle.id, access_status="BLACKLIST_ACTIVE", decision="DENY",
                              reason_code="BLACKLIST", policy_type="RESIDENTIAL", policy_version=1,
                              evaluated_at=now, response_status="NOT_CONFIGURED", barrier_intent="NONE",
                              physical_execution="NOT_IMPLEMENTED")
    db.add(decision); db.flush()
    db.add_all((
        BarrierAction(camera_id=entry.id, site_id=site.id, recognition_event_id=event.id,
                      access_decision_id=decision.id, action_type="OPEN", trigger_source="MANUAL_RELEASE",
                      command_status="CAMERA_ACKNOWLEDGED", requested_at=now, expires_at=now + timedelta(seconds=10),
                      requested_by_user_id=user.id, operator_reason="Verified visitor"),
        RecognitionPlateCorrection(recognition_event_id=event.id, corrected_plate="ABC1234",
                                   corrected_plate_normalized="ABC1234", reason="Image review",
                                   created_by_user_id=user.id, created_at=now),
        ParkingReconciliation(site_id=site.id, action_type="RESOLVE_WITHOUT_MATCH", action_token="report-test",
                              primary_recognition_event_id=event.id, previous_state={}, resulting_state={},
                              reason="Historical correction", performed_by_user_id=user.id, performed_at=now),
        OfflineRecoveryRun(camera_id=entry.id, status="COMPLETED", initial_reported_count=4,
                           last_reported_count=0, requested_max_count=100, requested_min_id=0,
                           messages_received=4, events_inserted=3, duplicates_skipped=1, invalid_skipped=0,
                           started_at=now, completed_at=now + timedelta(seconds=2)),
    ))
    db.commit(); service = ReportingService(db)
    query = service.query(site_id=site.id, period="custom", date_from="2026-10-15", date_to="2026-10-15")
    access = service.access(query); operations = service.operations(query)
    assert access["counts"]["DENY"] == 1 and access["blacklist"] == 1
    assert operations["manual_releases"] == 1
    assert operations["corrections"] == 1 and operations["reconciliations"] == 1
    assert operations["recovery"] == {"runs": 1, "imported": 3, "duplicates": 1, "skipped": 0, "failures": 0}
    assert db.scalar(select(AccessDecision).where(AccessDecision.id == decision.id)).decision == "DENY"


def test_reliability_uses_observed_events_and_segment_evidence(db: Session) -> None:
    site, entry, _, _, _ = seed_site(db)
    start = datetime(2026, 10, 15, 0, tzinfo=timezone.utc)
    db.add_all((
        UnifiedEvent(event_type="CAMERA_OFFLINE", category="SYSTEM_HEALTH", severity="WARNING",
                     attention_required=True, occurred_at=start, site_id=site.id, camera_id=entry.id,
                     source_type="HealthTransition", source_id="offline", display_subject=entry.code,
                     summary="Camera offline", audit_significant=False),
        UnifiedEvent(event_type="RECORDING_GAP", category="SYSTEM_HEALTH", severity="WARNING",
                     attention_required=True, occurred_at=start + timedelta(minutes=20), site_id=site.id,
                     source_type="HealthTransition", source_id="gap", display_subject="Recording",
                     summary="Recording gap from 2026-10-15T00:10:00+00:00 to 2026-10-15T00:20:00+00:00",
                     audit_significant=False),
        OperationalAlert(alert_type="CAMERA_OFFLINE", severity="WARNING", status="RESOLVED",
                         site_id=site.id, component_type="CAMERA", component_id=str(entry.id), fingerprint="camera-report",
                         title="Camera offline", summary="Observed", first_seen_at=start, last_seen_at=start + timedelta(minutes=5),
                         opened_at=start, resolved_at=start + timedelta(minutes=5), occurrence_count=1),
    ))
    source = MediaSource(camera_id=entry.id, provider_type="CENTRAL_RECORDER", enabled=True,
                         recording_enabled=True, recorder_state="RECORDING")
    db.add(source); db.flush()
    db.add_all((
        RecordingSegment(media_source_id=source.id, camera_id=entry.id, started_at=start,
                         ended_at=start + timedelta(minutes=10), relative_path="one.mp4", state="COMPLETE"),
        RecordingSegment(media_source_id=source.id, camera_id=entry.id, started_at=start + timedelta(minutes=20),
                         ended_at=start + timedelta(minutes=30), relative_path="two.mp4", state="COMPLETE"),
    ))
    db.commit(); service = ReportingService(db)
    query = service.query(site_id=site.id, period="custom", date_from="2026-10-15", date_to="2026-10-15")
    data = service.reliability(query)
    assert data["camera_incidents"] == 1 and data["gap_count"] == 1
    assert data["gap_duration"] == "10m"
    assert data["coverage"]["percent"] == 66.7
    assert data["coverage"]["gap_count"] == 1
    assert "uptime" not in str(data).lower()
