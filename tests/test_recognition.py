import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.integrations.visionzenith.recognition import image_bytes, parse_recognition
from app.integrations.visionzenith.topics import QUICK_RECOGNITION, RECOGNITION
from app.models.camera import Camera
from app.models.access_list_entry import AccessListEntry
from app.models.recognition_event import RecognitionEvent
from app.models.site import Site
from app.models.vehicle import Vehicle
from app.services.camera_service import CREDENTIALS_REVOKED
from app.services.recognition_ingestion_service import (
    RecognitionIngestionRejected,
    RecognitionIngestionService,
    RecognitionWorker,
)
from app.services.recognition_media_service import (
    RecognitionMediaError,
    RecognitionMediaService,
)
from app.services.recognition_service import (
    LiveCursorError,
    RecognitionService,
    decode_cursor,
    encode_cursor,
)


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aI1sAAAAASUVORK5CYII="
)


def create_camera(
    db: Session,
    *,
    serial: str = "275688a3-5c6d744b",
    enabled: bool = True,
    revoked: bool = False,
) -> Camera:
    site = db.scalar(select(Site).where(Site.code == "MAIN"))
    if site is None:
        site = Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur")
        db.add(site)
        db.flush()
    camera = Camera(
        site_id=site.id,
        code=f"CAM-{serial[-4:]}",
        name="RA5",
        direction="ENTRY",
        serial_number=serial,
        is_enabled=enabled,
        provisioning_state=(CREDENTIALS_REVOKED if revoked else "PROVISIONED"),
    )
    db.add(camera)
    db.commit()
    return camera


def payload(
    plate: str = "VAB1234",
    *,
    event_id: str | None = "event-1",
    captured_at: datetime | str | None = None,
    confidence=97.4,
    image: bytes | None = None,
    extra: dict | None = None,
) -> bytes:
    result = {
        "license": base64.b64encode(plate.encode("gbk")).decode(),
        "unique_id": event_id,
        "confidence": confidence,
        "direction": "ENTRY",
        "vehicle_color": "silver",
        "secret": "must-not-persist",
    }
    if captured_at is not None:
        result["timestamp"] = (
            captured_at.isoformat() if isinstance(captured_at, datetime) else captured_at
        )
    if image is not None:
        result["full_image_content"] = base64.b64encode(image).decode()
        result["small_image_content"] = base64.b64encode(image).decode()
    result.update(extra or {})
    document = {
        "id": event_id,
        "payload": {"AlarmInfoPlate": {"result": {"PlateResult": result}}},
    }
    return json.dumps(document).encode()


@pytest.fixture
def media_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "recognition_media_dir", tmp_path)
    return tmp_path


def test_quick_persists_and_full_enriches_same_event_without_second_action(
    db: Session, media_dir: Path
) -> None:
    camera = create_camera(db)
    now = datetime.now(timezone.utc)
    service = RecognitionIngestionService(db)

    quick, created, quick_display = service.process(
        serial_number=camera.serial_number,
        message_type=QUICK_RECOGNITION,
        payload=payload("WXY9876", captured_at=now, confidence=None),
        retained=False,
        received_at=now,
    )
    db.commit()
    full, full_created, full_display = service.process(
        serial_number=camera.serial_number,
        message_type=RECOGNITION,
        payload=payload("WXY9876", captured_at=now, image=PNG),
        retained=False,
        received_at=now + timedelta(seconds=1),
    )
    db.commit()

    assert created and quick_display
    assert not full_created and not full_display
    assert full.id == quick.id
    assert full.event_type == "ENRICHED"
    assert full.confidence == 97.4
    assert full.scene_image_path and full.plate_image_path
    assert db.scalar(select(RecognitionEvent).where(RecognitionEvent.id == quick.id))
    assert len(db.scalars(select(RecognitionEvent)).all()) == 1
    assert "must-not-persist" not in json.dumps(full.event_metadata)
    assert "iVBOR" not in json.dumps(full.event_metadata)


def test_camera_specific_and_ambiguous_fallback_correlation(db: Session, media_dir: Path) -> None:
    first_camera = create_camera(db, serial="SERIAL-ONE")
    second_camera = create_camera(db, serial="SERIAL-TWO")
    now = datetime.now(timezone.utc)
    service = RecognitionIngestionService(db)
    first, _, _ = service.process(
        serial_number=first_camera.serial_number,
        message_type=QUICK_RECOGNITION,
        payload=payload(event_id="same-id", captured_at=now),
        retained=False,
        received_at=now,
    )
    second, _, _ = service.process(
        serial_number=second_camera.serial_number,
        message_type=RECOGNITION,
        payload=payload(event_id="same-id", captured_at=now),
        retained=False,
        received_at=now,
    )
    assert first.id != second.id

    db.add(
        RecognitionEvent(
            camera_id=first_camera.id,
            site_id=first_camera.site_id,
            plate_text="VAB1234",
            direction="ENTRY",
            received_at=now,
            event_type="QUICK",
        )
    )
    db.flush()
    ambiguous, created, _ = service.process(
        serial_number=first_camera.serial_number,
        message_type=RECOGNITION,
        payload=payload(event_id=None, captured_at=now),
        retained=False,
        received_at=now + timedelta(seconds=1),
    )
    assert created
    assert ambiguous.id not in {first.id, second.id}


def test_replayed_vendor_event_is_enriched_without_retriggering_action(
    db: Session, media_dir: Path
) -> None:
    camera = create_camera(db)
    captured = datetime.now(timezone.utc) - timedelta(minutes=5)
    service = RecognitionIngestionService(db)
    original, created, should_display = service.process(
        serial_number=camera.serial_number,
        message_type=QUICK_RECOGNITION,
        payload=payload(captured_at=captured),
        retained=False,
        received_at=captured,
    )
    db.commit()

    replay, replay_created, replay_display = service.process(
        serial_number=camera.serial_number,
        message_type=RECOGNITION,
        payload=payload(captured_at=captured, image=PNG),
        retained=False,
        received_at=datetime.now(timezone.utc),
    )

    assert created and should_display
    assert replay.id == original.id
    assert not replay_created
    assert not replay_display
    assert replay.event_type == "ENRICHED"


@pytest.mark.parametrize("plate", ["V1", "WXY9876", "QAB4838E", "JVV12345"])
def test_variable_malaysian_plate_lengths(plate: str) -> None:
    assert parse_recognition(payload(plate)).plate == plate


@pytest.mark.parametrize("state", ["unknown", "disabled", "revoked", "retained"])
def test_invalid_camera_or_retained_event_rejected(
    db: Session, media_dir: Path, state: str
) -> None:
    serial = "UNKNOWN"
    retained = state == "retained"
    if state != "unknown":
        camera = create_camera(
            db,
            enabled=state != "disabled",
            revoked=state == "revoked",
        )
        serial = camera.serial_number
    with pytest.raises(RecognitionIngestionRejected):
        RecognitionIngestionService(db).process(
            serial_number=serial,
            message_type=QUICK_RECOGNITION,
            payload=payload(),
            retained=retained,
        )


def test_timestamps_confidence_and_allowlisted_metadata() -> None:
    parsed = parse_recognition(
        payload(
            captured_at="2026-10-04T12:00:00Z",
            confidence="98.5",
            extra={"plate_color": "white", "credential": "excluded"},
        )
    )
    assert parsed.captured_at == datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    assert parsed.confidence == 98.5
    assert parsed.metadata == {"plate_color": "white", "vehicle_color": "silver"}
    invalid = parse_recognition(payload(captured_at="bad", confidence=1000))
    assert invalid.captured_at is None
    assert invalid.confidence is None


def test_structured_ra5_capture_timestamp_takes_priority_over_delivery_time() -> None:
    captured_seconds = 1_790_348_406
    value = json.loads(payload(captured_at=None))
    result = value["payload"]["AlarmInfoPlate"]["result"]["PlateResult"]
    result["timeStamp"] = {
        "Timeval": {
            "decyear": 2026,
            "decmon": 9,
            "decday": 25,
            "dechour": 23,
            "decmin": 0,
            "decsec": 6,
            "sec": captured_seconds,
            "usec": 707531,
        }
    }
    value["timestamp"] = 1_791_384_455

    parsed = parse_recognition(json.dumps(value).encode())

    assert parsed.captured_at == datetime.fromtimestamp(
        captured_seconds + 0.707531, timezone.utc
    )


@pytest.mark.parametrize(
    ("vendor_value", "motion"),
    [(0, "UNKNOWN"), (1, "LEFT"), (2, "RIGHT"), (3, "UP"), (4, "DOWN")],
)
def test_vendor_motion_direction_is_normalized_separately_from_lane_direction(
    vendor_value: int, motion: str
) -> None:
    parsed = parse_recognition(payload(extra={"direction": vendor_value}))
    assert parsed.direction == motion
    assert parsed.metadata["motion_direction"] == motion


def test_operational_direction_comes_from_camera_configuration(
    db: Session, media_dir: Path
) -> None:
    camera = create_camera(db)
    event, _, _ = RecognitionIngestionService(db).process(
        serial_number=camera.serial_number,
        message_type=QUICK_RECOGNITION,
        payload=payload(extra={"direction": 1}),
        retained=False,
    )
    assert event.direction == "ENTRY"
    assert event.event_metadata["motion_direction"] == "LEFT"


def test_malformed_json_is_rejected_without_poisoning_later_processing(
    db: Session, media_dir: Path
) -> None:
    camera = create_camera(db)
    service = RecognitionIngestionService(db)
    with pytest.raises(RecognitionIngestionRejected):
        service.process(
            serial_number=camera.serial_number,
            message_type=QUICK_RECOGNITION,
            payload=b"not-json",
            retained=False,
        )

    event, created, _ = service.process(
        serial_number=camera.serial_number,
        message_type=QUICK_RECOGNITION,
        payload=payload("V1"),
        retained=False,
    )
    assert created
    assert event.plate_text == "V1"


@pytest.mark.parametrize(
    ("data", "extension"),
    [(b"\xff\xd8\xffx", ".jpg"), (b"\x89PNG\r\n\x1a\nx", ".png"), (b"BMx", ".bmp")],
)
def test_supported_image_signatures(data: bytes, extension: str) -> None:
    encoded = base64.b64encode(data).decode()
    assert image_bytes(encoded, max_decoded_bytes=100) == (data, extension)


def test_media_safe_filename_size_and_path_validation(
    media_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RecognitionMediaService(media_dir)
    event_id = uuid.uuid4()
    reference = service.store(event_id, "scene", base64.b64encode(PNG).decode())
    assert reference == f"{event_id}_scene.png"
    assert service.resolve(reference).read_bytes() == PNG
    with pytest.raises(RecognitionMediaError):
        service.resolve("../secret.png")
    monkeypatch.setattr(settings, "recognition_max_image_bytes", 4)
    with pytest.raises(RecognitionMediaError):
        service.store(event_id, "plate", base64.b64encode(PNG).decode())
    monkeypatch.setattr(settings, "recognition_max_image_bytes", 1024)
    with pytest.raises(RecognitionMediaError):
        RecognitionMediaService(media_dir).store(
            event_id,
            "plate",
            base64.b64encode(b"not-an-image").decode(),
        )


def test_live_cursor_returns_enriched_stable_event(db: Session, media_dir: Path) -> None:
    camera = create_camera(db)
    now = datetime.now(timezone.utc)
    service = RecognitionIngestionService(db)
    event, _, _ = service.process(
        serial_number=camera.serial_number,
        message_type=QUICK_RECOGNITION,
        payload=payload(captured_at=now, confidence=None),
        retained=False,
        received_at=now,
    )
    db.commit()
    event.updated_at = now
    db.commit()
    cursor = encode_cursor(event.updated_at, event.id)
    assert decode_cursor(cursor) == (event.updated_at.astimezone(timezone.utc), event.id)
    service.process(
        serial_number=camera.serial_number,
        message_type=RECOGNITION,
        payload=payload(captured_at=now, image=PNG),
        retained=False,
        received_at=now + timedelta(seconds=1),
    )
    event.updated_at = now + timedelta(seconds=1)
    db.commit()

    changed, next_cursor = RecognitionService(db).changed_after(cursor)
    assert len(changed) == 1
    assert changed[0]["id"] == str(event.id)
    assert changed[0]["status"] == "Complete"
    assert next_cursor != cursor
    with pytest.raises(LiveCursorError):
        RecognitionService(db).changed_after("not-a-cursor")


def test_bounded_worker_queue_rejects_overflow(db: Session) -> None:
    worker = RecognitionWorker(
        session_factory=lambda: db,
        command_manager=object(),  # queue is not started in this boundary test
        queue_size=1,
    )
    assert worker.enqueue("serial", QUICK_RECOGNITION, payload(), retained=False)
    assert not worker.enqueue("serial", QUICK_RECOGNITION, payload(), retained=False)
    assert not worker.enqueue("serial", QUICK_RECOGNITION, payload(), retained=True)


def test_history_pagination_and_live_results_are_bounded(
    db: Session, media_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    camera = create_camera(db)
    now = datetime.now(timezone.utc)
    for number in range(105):
        db.add(
            RecognitionEvent(
                camera_id=camera.id,
                site_id=camera.site_id,
                vendor_event_id=f"event-{number}",
                plate_text=f"V{number}",
                direction="ENTRY" if number % 2 == 0 else "EXIT",
                captured_at=now + timedelta(microseconds=number),
                received_at=now + timedelta(microseconds=number),
                event_type="QUICK",
            )
        )
    db.commit()
    monkeypatch.setattr(settings, "recognition_page_size", 10)
    service = RecognitionService(db)

    page = service.history(page=1, direction="ENTRY")
    changed, cursor = service.changed_after(None, limit=1000)

    assert page.total == 53
    assert len(page.items) == 10
    assert page.page_count == 6
    assert len(changed) == 100
    assert cursor is not None


@pytest.mark.parametrize(
    ("list_type", "enabled", "valid_from", "valid_until", "expected_status", "expected_label"),
    [
        ("ALLOW", True, None, None, "WHITELIST", "Whitelist"),
        ("BLOCK", True, None, None, "BLACKLIST", "Blacklist"),
        ("ALLOW", False, None, None, "WHITELIST_DISABLED", "Whitelist · Disabled"),
        ("BLOCK", False, None, None, "BLACKLIST_DISABLED", "Blacklist · Disabled"),
        ("ALLOW", True, None, datetime.now(timezone.utc) - timedelta(minutes=1), "WHITELIST_EXPIRED", "Whitelist · Expired"),
        ("BLOCK", True, datetime.now(timezone.utc) + timedelta(hours=1), None, "BLACKLIST_NOT_YET_VALID", "Blacklist · Not Yet Valid"),
    ],
)
def test_recognition_access_status_is_resolved_from_central_entry(
    db: Session,
    list_type: str,
    enabled: bool,
    valid_from: datetime | None,
    valid_until: datetime | None,
    expected_status: str,
    expected_label: str,
) -> None:
    camera = create_camera(db)
    event = RecognitionEvent(
        camera_id=camera.id,
        site_id=camera.site_id,
        plate_text="VAB1234",
        received_at=datetime.now(timezone.utc),
        event_type="QUICK",
    )
    vehicle = Vehicle(plate_text="VAB1234")
    db.add_all([
        event,
        AccessListEntry(
            vehicle=vehicle,
            site_id=camera.site_id,
            list_type=list_type,
            enabled=enabled,
            valid_from=valid_from,
            valid_until=valid_until,
        ),
    ])
    db.commit()

    result = RecognitionService(db).history().items[0]

    assert result["access_status"] == expected_status
    assert result["access_label"] == expected_label
    assert result["access_entry_id"] is not None


def test_recognition_access_status_is_site_specific_and_not_listed_is_explicit(
    db: Session,
) -> None:
    first_camera = create_camera(db, serial="SITE-ONE")
    second_site = Site(code="SECOND", name="Second Site", timezone="Asia/Kuala_Lumpur")
    second_camera = Camera(
        site=second_site,
        code="SECOND-ENTRY-01",
        name="Second Entry",
        direction="ENTRY",
        serial_number="SITE-TWO",
        is_enabled=True,
        provisioning_state="PROVISIONED",
    )
    vehicle = Vehicle(plate_text="SITE123")
    db.add_all([second_camera, vehicle])
    db.flush()
    db.add_all([
        AccessListEntry(vehicle=vehicle, site_id=first_camera.site_id, list_type="ALLOW", enabled=True),
        AccessListEntry(vehicle=vehicle, site_id=second_site.id, list_type="BLOCK", enabled=True),
        RecognitionEvent(camera_id=first_camera.id, site_id=first_camera.site_id, plate_text="SITE123", received_at=datetime.now(timezone.utc), event_type="QUICK"),
        RecognitionEvent(camera_id=second_camera.id, site_id=second_site.id, plate_text="SITE123", received_at=datetime.now(timezone.utc), event_type="QUICK"),
        RecognitionEvent(camera_id=first_camera.id, site_id=first_camera.site_id, plate_text="NONE123", received_at=datetime.now(timezone.utc), event_type="QUICK"),
    ])
    db.commit()

    items = RecognitionService(db).history().items
    statuses = {(item["plate"], item["site"]): item["access_status"] for item in items}

    assert statuses[("SITE123", "MAIN")] == "WHITELIST"
    assert statuses[("SITE123", "SECOND")] == "BLACKLIST"
    assert statuses[("NONE123", "MAIN")] == "NOT_LISTED"


def test_quick_full_enrichment_does_not_create_or_change_access_entries(
    db: Session, media_dir: Path
) -> None:
    camera = create_camera(db)
    vehicle = Vehicle(plate_text="WXY9876")
    entry = AccessListEntry(
        vehicle=vehicle,
        site_id=camera.site_id,
        list_type="ALLOW",
        enabled=True,
    )
    db.add(entry)
    db.commit()
    now = datetime.now(timezone.utc)
    ingestion = RecognitionIngestionService(db)

    ingestion.process(
        serial_number=camera.serial_number,
        message_type=QUICK_RECOGNITION,
        payload=payload("WXY9876", captured_at=now),
        retained=False,
        received_at=now,
    )
    ingestion.process(
        serial_number=camera.serial_number,
        message_type=RECOGNITION,
        payload=payload("WXY9876", captured_at=now, image=PNG),
        retained=False,
        received_at=now + timedelta(seconds=1),
    )
    db.commit()

    entries = db.scalars(select(AccessListEntry)).all()
    assert entries == [entry]
    assert entry.list_type == "ALLOW"
    assert RecognitionService(db).latest()["access_status"] == "WHITELIST"
