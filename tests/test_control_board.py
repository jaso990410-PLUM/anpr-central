from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.integrations.visionzenith.protocol import display_settings_frame, idle_frame, led_frame, traffic_light_frame
from app.models.camera import Camera
from app.models.site import Site
from app.models.parking import AccessDecision, DecisionResponseProfile
from app.models.recognition_event import RecognitionEvent
from app.services.control_board_runtime_service import ControlBoardRuntimeService, PeripheralContext, PeripheralRuntimeCoordinator
from app.services.control_board_service import ControlBoardService, ControlBoardValidationError
from app.services.peripheral_preset_service import PeripheralPresetService, PeripheralPresetValidationError


def camera(db: Session) -> Camera:
    site = Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur", is_active=True)
    item = Camera(site=site, code="ENTRY-1", name="Main Entry", direction="ENTRY", serial_number="RA5-CB-1", is_enabled=True)
    db.add(item)
    db.commit()
    return item


def preset(db: Session):
    item = PeripheralPresetService(db).create(name="Normal Operations", description="Default", enabled=True)
    values = {"name": item.name, "description": item.description, "enabled": "true", "busy_periods": "0,08:00,17:00"}
    for mode in ("idle", "busy"):
        values.update({f"{mode}_traffic_state": "RED" if mode == "idle" else "GREEN", f"{mode}_green_seconds": "8", f"{mode}_speaker_enabled": "true", f"{mode}_speaker_volume": "7", f"{mode}_speaker_speed": "5", f"{mode}_speaker_pitch": "5", f"{mode}_speaker_voice": "3", f"{mode}_plate_pause_ms": "100", f"{mode}_speaker_message": "Welcome {plate}"})
        for number in range(1, 5):
            values.update({f"{mode}_row{number}_type": "CUSTOM" if number == 1 else "STANDARD", f"{mode}_row{number}_source": "CAMERA_NAME", f"{mode}_row{number}_text": f"ROW {number}"})
    PeripheralPresetService(db).update(item, values)
    db.commit()
    return item


def test_protocol_honors_documented_slave_address() -> None:
    assert led_frame("ANPR", address=7)[3] == 7
    assert idle_frame(1, "READY", 1, address=8)[3] == 8
    assert traffic_light_frame(red=True, address=9)[3] == 9
    with pytest.raises(ValueError):
        led_frame("ANPR", address=256)
    assert display_settings_frame(5, 5, sequence=3, address=100).hex().upper() == "AA55036400F1000205050893AF"
    with pytest.raises(ValueError):
        display_settings_frame(6, 5)


def test_create_one_board_per_camera_and_validate_values(db: Session) -> None:
    cam = camera(db)
    service = ControlBoardService(db)
    board, created = service.save(cam, enabled=True, serial_channel="1", baud_rate="9600", data_bits="8", parity="none", stop_bits="1", slave_id="7")
    assert created and board.serial_channel == 1 and board.slave_id == 7
    assert board.communication_sync_status == "PENDING_SYNC"
    db.commit()
    updated, created = service.save(cam, enabled=False, serial_channel="0", baud_rate="19200", data_bits="8", parity="even", stop_bits="1", slave_id="1")
    assert not created and not updated.enabled and updated.parity == "EVEN"
    with pytest.raises(ControlBoardValidationError):
        service.save(cam, enabled=True, serial_channel="2", baud_rate="9600", data_bits="8", parity="none", stop_bits="1", slave_id="1")
    with pytest.raises(ControlBoardValidationError):
        service.save(cam, enabled=True, serial_channel="0", baud_rate="12345", data_bits="8", parity="none", stop_bits="1", slave_id="1")


def test_preset_modes_rows_busy_period_and_runtime_resolution(db: Session) -> None:
    cam = camera(db)
    configured = preset(db)
    board, _ = ControlBoardService(db).save(cam, enabled=True, serial_channel=0, baud_rate=9600, data_bits=8, parity="NONE", stop_bits=1, slave_id=1, default_preset_id=configured.id)
    db.commit()
    resolved = ControlBoardRuntimeService(db).resolve(board, at=datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc), context=PeripheralContext(plate="QAB4838E", access_status="ALLOWED"))
    assert resolved.mode == "BUSY" and resolved.traffic_state == "GREEN"
    assert resolved.led_brightness == 5 and resolved.led_scroll_speed == 5
    assert resolved.rows[0] == "ROW 1" and resolved.rows[1] == "Main Entry"
    assert resolved.speaker_message == "Welcome Q[p100]A[p100]B[p100]4[p100]8[p100]3[p100]8[p100]E"


def test_preset_rejects_overlapping_periods_and_invalid_gbk_length(db: Session) -> None:
    item = PeripheralPresetService(db).create(name="Test")
    with pytest.raises(PeripheralPresetValidationError, match="overlap"):
        PeripheralPresetService(db).update(item, {"name": "Test", "busy_periods": "0,08:00,12:00\n0,11:00,13:00"})
    values = {"name": "Test", "busy_periods": ""}
    for mode in ("idle", "busy"):
        for number in range(1, 5):
            values.update({f"{mode}_row{number}_type": "CUSTOM", f"{mode}_row{number}_source": "BLANK", f"{mode}_row{number}_text": "A" * 256})
    with pytest.raises(PeripheralPresetValidationError, match="255 GBK"):
        PeripheralPresetService(db).update(item, values)


def test_schedule_overlap_rejected(db: Session) -> None:
    cam = camera(db)
    configured = preset(db)
    service = ControlBoardService(db)
    board, _ = service.save(cam, enabled=True, serial_channel=0, baud_rate=9600, data_bits=8, parity="NONE", stop_bits=1, slave_id=1, default_preset_id=configured.id)
    service.add_schedule(board, preset_id=configured.id, starts_at="2026-10-05T08:00", ends_at="2026-10-05T10:00", enabled=True)
    with pytest.raises(ControlBoardValidationError, match="overlap"):
        service.add_schedule(board, preset_id=configured.id, starts_at="2026-10-05T09:00", ends_at="2026-10-05T11:00", enabled=True)


def test_recognition_uses_board_channel_speaker_settings_and_schedules_restore(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    cam = camera(db)
    cam.connection_state = "ONLINE"
    configured = preset(db)
    board, _ = ControlBoardService(db).save(cam, enabled=True, serial_channel=1, baud_rate=9600, data_bits=8, parity="NONE", stop_bits=1, slave_id=7, default_preset_id=configured.id)
    db.commit()
    calls = []
    class Commands:
        def enqueue_serial(self, **kwargs): calls.append(kwargs)
    coordinator = PeripheralRuntimeCoordinator(session_factory=sessionmaker(bind=db.get_bind(), expire_on_commit=False), command_manager=Commands(), is_connected=lambda: True)
    restores = []
    monkeypatch.setattr(coordinator, "schedule_restore", lambda camera_id, delay_seconds=5.0: restores.append((camera_id, delay_seconds)))
    coordinator.handle_recognition(cam.id, "QAB4838E")
    assert calls[0]["command_type"] == "preset_speaker"
    assert calls[0]["serial_channel"] == 1
    assert calls[0]["raw_frame"].startswith(b"\xfd")
    assert restores == [(cam.id, 5.0)]


def test_decision_response_applies_led_settings_and_site_time_sources(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    cam = camera(db)
    cam.connection_state = "ONLINE"
    board, _ = ControlBoardService(db).save(cam, enabled=True, serial_channel=0, baud_rate=9600, data_bits=8, parity="NONE", stop_bits=1, slave_id=1)
    profile = DecisionResponseProfile(
        site_id=cam.site_id, name="Allowed", response_kind="ALLOW",
        led_rows=[
            {"type": "STANDARD", "source": "CURRENT_DATE"},
            {"type": "STANDARD", "source": "CURRENT_TIME"},
            {"type": "STANDARD", "source": "CURRENT_WEEKDAY"},
            {"type": "STANDARD", "source": "CURRENT_WEEK_NUMBER"},
        ],
        led_brightness=3, led_scroll_speed=2, display_seconds=5,
        traffic_state="GREEN", traffic_green_seconds=5, speaker_enabled=False,
        speaker_message="{plate}", speaker_volume=7, speaker_speed=5,
        speaker_voice=3, speaker_pitch=5, plate_pause_ms=100, enabled=True,
    )
    captured = datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc)
    event = RecognitionEvent(camera_id=cam.id, site_id=cam.site_id, plate_text="QAB4838E", source="LIVE", received_at=captured, captured_at=captured, event_type="FULL")
    db.add_all([profile, event]); db.flush()
    decision = AccessDecision(
        recognition_event_id=event.id, site_id=cam.site_id, camera_id=cam.id,
        access_status="WHITELIST_ACTIVE", decision="ALLOW", reason_code="ACCESS_ALLOWED",
        policy_type="RESIDENTIAL", policy_version=1, evaluated_at=captured,
        response_profile_id=profile.id,
    )
    db.add(decision); db.commit()
    calls = []
    class Commands:
        def enqueue_serial(self, **kwargs): calls.append(kwargs)
    coordinator = PeripheralRuntimeCoordinator(session_factory=sessionmaker(bind=db.get_bind(), expire_on_commit=False), command_manager=Commands(), is_connected=lambda: True)
    monkeypatch.setattr(coordinator, "schedule_restore", lambda *_args, **_kwargs: None)
    coordinator.apply_decision_response(decision.id)
    assert calls[0]["command_type"] == "decision_led_settings"
    assert calls[0]["raw_frame"] == display_settings_frame(3, 2, address=board.slave_id)
    expected = ("05/10/2026", "09:00:00", "Monday", "Week 41")
    assert [item["raw_frame"] for item in calls[1:5]] == [idle_frame(number, value, number, address=board.slave_id) for number, value in enumerate(expected, 1)]
