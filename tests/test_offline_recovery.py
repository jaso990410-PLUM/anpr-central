import base64
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.integrations.visionzenith.protocol import offline_recovery_envelope
from app.integrations.visionzenith.topics import (
    OFFLINE_COUNT,
    OFFLINE_RECOGNITION,
    OFFLINE_RECOVERY_REPLY,
    SUBSCRIPTIONS,
    offline_recovery_topic,
    parse_native_topic,
)
from app.models.camera import Camera
from app.models.offline_recovery_run import OfflineRecoveryRun
from app.models.recognition_event import RecognitionEvent
from app.models.site import Site
from app.services.offline_recovery_service import (
    OfflineRecoveryCoordinator,
    OfflineRecoveryRejected,
    parse_offline_count,
    parse_recovery_ack,
)
from app.services.recognition_ingestion_service import RecognitionIngestionService
from app.services.recognition_ingestion_service import RecognitionWorker


def camera(db: Session, *, serial: str = "RA5-OFFLINE", state: str = "ONLINE") -> Camera:
    site = db.scalar(select(Site).where(Site.code == "MAIN"))
    if site is None:
        site = Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur")
        db.add(site)
        db.flush()
    value = Camera(
        site=site,
        code=f"CAM-{serial}",
        name=serial,
        direction="ENTRY",
        serial_number=serial,
        is_enabled=True,
        provisioning_state="PROVISIONED",
        connection_state=state,
    )
    db.add(value)
    db.commit()
    return value


def count_payload(count: object) -> bytes:
    return json.dumps({"payload": {"body": {"offline_record": count}}}).encode()


def recognition_payload(
    *, event_id: str, record_id: str, captured_at: datetime, plate: str = "WQT4120"
) -> bytes:
    result = {
        "license": base64.b64encode(plate.encode("gbk")).decode(),
        "unique_id": event_id,
        "plateid": record_id,
        "timestamp": captured_at.isoformat(),
        "confidence": 98.5,
    }
    return json.dumps(
        {"id": event_id, "payload": {"AlarmInfoPlate": {"result": {"PlateResult": result}}}}
    ).encode()


class CommandManagerStub:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def enqueue_offline_recovery(self, **values):
        request_id = f"OFFLINE-TEST-{len(self.requests) + 1}"
        self.requests.append({**values, "request_id": request_id})
        return SimpleNamespace(request_id=request_id)


def coordinator(db: Session, manager: CommandManagerStub, *, connected: bool = True):
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    return OfflineRecoveryCoordinator(
        session_factory=factory,
        command_manager=manager,  # type: ignore[arg-type]
        is_connected=lambda: connected,
    )


def test_offline_topics_and_push_envelope_are_documented_and_non_destructive() -> None:
    subscribed = {topic for topic, _qos in SUBSCRIPTIONS}
    assert "device/+/message/up/offline_record" in subscribed
    assert "device/+/message/up/ivs_result_offline" in subscribed
    assert "device/+/message/down/check_offline_record/reply" in subscribed
    assert parse_native_topic("device/SN/message/up/offline_record").message_type == OFFLINE_COUNT
    assert parse_native_topic("device/SN/message/up/ivs_result_offline").message_type == OFFLINE_RECOGNITION
    assert parse_native_topic("device/SN/message/down/check_offline_record/reply").message_type == OFFLINE_RECOVERY_REPLY
    assert offline_recovery_topic("SN") == "device/SN/message/down/check_offline_record"

    envelope = offline_recovery_envelope(
        request_id="request-1", serial_number="SN", bv=1, max_count=100
    )
    assert envelope["payload"] == {
        "type": "check_offline_record",
        "body": {"enable": "push", "max_count": 100, "min_id": 0},
    }
    assert "clear" not in json.dumps(envelope).lower()


@pytest.mark.parametrize("value", [-1, "3", None, True, [], 10_000_001])
def test_offline_count_rejects_invalid_values(value: object) -> None:
    with pytest.raises(OfflineRecoveryRejected):
        parse_offline_count(count_payload(value))
    with pytest.raises(OfflineRecoveryRejected):
        parse_offline_count(count_payload(1), retained=True)


def test_recovery_state_progress_completion_and_stall(db: Session) -> None:
    target = camera(db)
    manager = CommandManagerStub()
    service = coordinator(db, manager)

    run = service.handle_count(target.serial_number, count_payload(150))
    assert run.status == "REQUESTED"
    assert manager.requests[-1]["max_count"] == settings.offline_recovery_batch_size
    assert manager.requests[-1]["min_id"] == 0

    service.handle_ack(
        target.serial_number,
        json.dumps({"id": run.command_request_id, "code": "200"}).encode(),
    )
    service.record_message(target.serial_number, True, True)
    service.record_message(target.serial_number, False, True)
    service.record_message(target.serial_number, False, False)
    progressed = service.handle_count(target.serial_number, count_payload(50))
    assert progressed.status == "REQUESTED"
    assert len(manager.requests) == 2

    completed = service.handle_count(target.serial_number, count_payload(0))
    assert completed.status == "COMPLETED"
    assert completed.events_inserted == 1
    assert completed.duplicates_skipped == 1
    assert completed.invalid_skipped == 1

    stalled = service.handle_count(target.serial_number, count_payload(8))
    assert stalled.status == "REQUESTED"
    stalled = service.handle_count(target.serial_number, count_payload(8))
    assert stalled.status == "STALLED"
    assert len(manager.requests) == 3


def test_zero_backlog_does_not_create_or_request_a_recovery(db: Session) -> None:
    target = camera(db)
    manager = CommandManagerStub()
    service = coordinator(db, manager)
    assert service.handle_count(target.serial_number, count_payload(0)) is None
    assert manager.requests == []
    assert db.scalar(select(OfflineRecoveryRun)) is None


def test_recovery_rejects_unknown_disabled_and_retained_camera(db: Session) -> None:
    manager = CommandManagerStub()
    service = coordinator(db, manager)
    with pytest.raises(OfflineRecoveryRejected):
        service.handle_count("UNKNOWN", count_payload(1))
    target = camera(db)
    target.is_enabled = False
    db.commit()
    with pytest.raises(OfflineRecoveryRejected):
        service.handle_count(target.serial_number, count_payload(1))
    with pytest.raises(OfflineRecoveryRejected):
        service.handle_count(target.serial_number, count_payload(1), retained=True)


def test_recovery_timeout_offline_and_restart_are_uncertain_or_safe(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = camera(db)
    manager = CommandManagerStub()
    service = coordinator(db, manager)
    run = service.handle_count(target.serial_number, count_payload(3))
    stored = db.get(OfflineRecoveryRun, run.id)
    stored.started_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    db.commit()
    monkeypatch.setattr(settings, "offline_recovery_timeout_seconds", 10)
    assert service.mark_timeouts() == 1
    db.expire_all()
    assert db.get(OfflineRecoveryRun, run.id).status == "TIMEOUT_UNCERTAIN"

    run = service.handle_count(target.serial_number, count_payload(2))
    target.connection_state = "OFFLINE"
    db.commit()
    assert service.mark_camera_offline_runs() == 1
    db.expire_all()
    assert db.get(OfflineRecoveryRun, run.id).status == "CAMERA_OFFLINE"

    run = service.handle_count(target.serial_number, count_payload(1))
    service.start()
    db.expire_all()
    restarted = db.get(OfflineRecoveryRun, run.id)
    assert restarted.status == "TIMEOUT_UNCERTAIN"
    assert "restarted" in restarted.last_error.lower()


def test_recovered_event_preserves_time_source_and_deduplicates_live_event(
    db: Session,
) -> None:
    target = camera(db)
    captured = datetime.now(timezone.utc) - timedelta(hours=2)
    received = datetime.now(timezone.utc)
    payload = recognition_payload(
        event_id="event-offline-1", record_id="42", captured_at=captured
    )
    event, created, should_display = RecognitionIngestionService(db).process(
        serial_number=target.serial_number,
        message_type=OFFLINE_RECOGNITION,
        payload=payload,
        retained=False,
        received_at=received,
    )
    db.commit()
    assert created and not should_display
    assert event.source == "OFFLINE_RECOVERY"
    assert event.vendor_record_id == "42"
    assert event.captured_at == captured
    assert event.received_at == received
    assert event.recovered_at == received

    duplicate, duplicate_created, duplicate_display = RecognitionIngestionService(db).process(
        serial_number=target.serial_number,
        message_type=OFFLINE_RECOGNITION,
        payload=payload,
        retained=False,
        received_at=received + timedelta(minutes=1),
    )
    assert duplicate.id == event.id
    assert not duplicate_created and not duplicate_display
    assert len(db.scalars(select(RecognitionEvent)).all()) == 1


def test_recovered_copy_enriches_live_without_changing_live_origin(db: Session) -> None:
    target = camera(db)
    captured = datetime.now(timezone.utc) - timedelta(minutes=3)
    live = RecognitionEvent(
        camera_id=target.id,
        site_id=target.site_id,
        vendor_event_id="shared-event",
        plate_text="WQT4120",
        direction="ENTRY",
        captured_at=captured,
        received_at=captured,
        event_type="QUICK",
        source="LIVE",
    )
    db.add(live)
    db.commit()
    recovered, created, should_display = RecognitionIngestionService(db).process(
        serial_number=target.serial_number,
        message_type=OFFLINE_RECOGNITION,
        payload=recognition_payload(
            event_id="shared-event", record_id="91", captured_at=captured
        ),
        retained=False,
        received_at=datetime.now(timezone.utc),
    )
    assert recovered.id == live.id
    assert not created and not should_display
    assert recovered.source == "LIVE"
    assert recovered.vendor_record_id == "91"
    assert recovered.confidence == 98.5


def test_recovery_worker_never_invokes_physical_output_hooks(
    db: Session, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = camera(db)
    monkeypatch.setattr(settings, "recognition_media_dir", tmp_path)
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    results: list[tuple[str, bool, bool]] = []

    class NoPhysicalCommands:
        def enqueue_serial(self, **_values):
            raise AssertionError("Recovered recognition queued a physical command")

    worker = RecognitionWorker(
        session_factory=factory,
        command_manager=NoPhysicalCommands(),  # type: ignore[arg-type]
        peripheral_event_callback=lambda *_args: (_ for _ in ()).throw(
            AssertionError("Recovered recognition invoked a peripheral hook")
        ),
        recovery_result_callback=lambda serial, created, valid: results.append(
            (serial, created, valid)
        ),
    )
    worker.start()
    try:
        assert worker.enqueue(
            target.serial_number,
            OFFLINE_RECOGNITION,
            recognition_payload(
                event_id="historical-no-output",
                record_id="777",
                captured_at=datetime.now(timezone.utc) - timedelta(hours=1),
            ),
            retained=False,
        )
        worker.drain()
    finally:
        worker.stop()
    assert results == [(target.serial_number, True, True)]


def test_non_200_ack_and_malformed_ack_are_not_completion(db: Session) -> None:
    target = camera(db)
    manager = CommandManagerStub()
    service = coordinator(db, manager)
    run = service.handle_count(target.serial_number, count_payload(2))
    rejected = service.handle_ack(
        target.serial_number,
        json.dumps({"id": run.command_request_id, "code": 500}).encode(),
    )
    assert rejected.status == "ERROR"
    with pytest.raises(OfflineRecoveryRejected):
        parse_recovery_ack(b"not-json")
