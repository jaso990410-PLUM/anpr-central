from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker
from uuid import uuid4

from app.integrations.visionzenith.access_list import (
    delete_all_camera_access_records,
    delete_camera_access_record,
    desired_changes,
    merge_record,
    readback_status,
    select_record,
)
from app.models.access_list_camera_sync import AccessListCameraSync
from app.models.access_list_entry import AccessListEntry
from app.models.camera import Camera
from app.models.site import Site
from app.models.vehicle import Vehicle
from app.models.camera_command import CameraCommand
from app.services.access_list_service import (
    AccessListService,
    AccessListValidationError,
    normalize_plate,
    normalize_reference_name,
)
from app.services.access_list_sync_service import AccessListSyncCoordinator, AccessSyncTask


class FakeCommandManager:
    def __init__(self, factory, replies):
        self.factory = factory
        self.replies = list(replies)
        self.calls = []

    def enqueue_access_list(self, *, camera_id, serial_number, command_type, body, request_metadata):
        self.calls.append((camera_id, command_type, body))
        reply = self.replies.pop(0)
        with self.factory() as db:
            command = CameraCommand(
                camera_id=camera_id,
                request_id=f"test-{uuid4().hex}",
                command_type=command_type,
                status=reply.get("status", "CAMERA_ACKNOWLEDGED"),
                acknowledged_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
                response_metadata={"access_list_body": reply.get("body", {"state_code": 200})},
                error_detail=reply.get("error"),
                request_metadata=request_metadata,
            )
            db.add(command)
            db.commit()
            db.refresh(command)
            db.expunge(command)
            return command


def site_and_camera(db: Session, *, participating: bool = True) -> tuple[Site, Camera]:
    site = Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur", is_active=True)
    camera = Camera(
        site=site,
        code="MAIN-ENTRY-01",
        name="Main Entry",
        direction="ENTRY",
        serial_number="RA5-ACCESS-TEST",
        is_enabled=True,
        access_list_sync_enabled=participating,
    )
    db.add(camera)
    db.commit()
    return site, camera


@pytest.mark.parametrize("plate", ["v1", " WQT4120 ", "QAB4838E", "JVV12345"])
def test_plate_normalization_supports_variable_lengths(plate: str) -> None:
    assert normalize_plate(plate) == plate.strip().upper()


def test_single_delete_cannot_escalate_to_delete_all() -> None:
    for invalid in (None, "", "   "):
        with pytest.raises(ValueError, match="explicit plate"):
            delete_camera_access_record(invalid)
    assert delete_camera_access_record(" wqt4120 ") == {"operator_type": "delete", "plate": "WQT4120"}
    assert delete_all_camera_access_records() == {"operator_type": "delete"}


def test_create_entry_is_site_specific_and_rejects_duplicate(db: Session) -> None:
    site, camera = site_and_camera(db)
    service = AccessListService(db)
    entry = service.create(
        plate=" wqt4120 ",
        site_id=site.id,
        reference_name="  Nur Aisyah  ",
        list_type="ALLOW",
        enabled=True,
        camera_comment="Staff",
    )
    db.commit()

    assert entry.vehicle.plate_text == "WQT4120"
    assert entry.vehicle.reference_name == "Nur Aisyah"
    assert entry.list_type == "ALLOW" and entry.enabled
    sync = db.scalar(select(AccessListCameraSync))
    assert sync is not None and sync.camera_id == camera.id and sync.status == "PENDING"
    with pytest.raises(AccessListValidationError, match="already"):
        service.create(plate="WQT4120", site_id=site.id, list_type="BLOCK", enabled=False)


def test_enable_and_classification_are_independent(db: Session) -> None:
    site, _ = site_and_camera(db)
    service = AccessListService(db)
    entry = service.create(plate="VAB1234", site_id=site.id, list_type="BLOCK", enabled=True)
    service.set_enabled(entry, False)
    assert entry.list_type == "BLOCK"
    assert not entry.enabled


def test_vehicle_reference_name_can_be_updated_and_cleared(db: Session) -> None:
    site, _ = site_and_camera(db)
    service = AccessListService(db)
    entry = service.create(
        plate="VAB1234",
        site_id=site.id,
        reference_name="Security Pool Car",
        list_type="ALLOW",
        enabled=True,
    )
    service.update(entry, reference_name="  Operations Vehicle  ", list_type="ALLOW", enabled=True)
    assert entry.vehicle.reference_name == "Operations Vehicle"
    service.update(entry, reference_name="", list_type="ALLOW", enabled=True)
    assert entry.vehicle.reference_name is None
    with pytest.raises(AccessListValidationError, match="120 characters"):
        normalize_reference_name("N" * 121)


@pytest.mark.parametrize(
    "values,message",
    [
        ({"valid_from": "2026-10-05T10:00", "valid_until": "2026-10-05T09:00"}, "later"),
        ({"daily_window_enabled": True, "daily_start": "06:00", "daily_end": "06:00"}, "must differ"),
        ({"daily_window_enabled": True, "daily_start": "", "daily_end": "18:00"}, "required"),
        ({"camera_comment": "12345678901234567"}, "16 characters"),
    ],
)
def test_entry_validation(db: Session, values: dict, message: str) -> None:
    site, _ = site_and_camera(db)
    defaults = {"plate": "ABC1234", "site_id": site.id, "list_type": "ALLOW", "enabled": True}
    with pytest.raises(AccessListValidationError, match=message):
        AccessListService(db).create(**defaults, **values)


def test_vendor_mapping_and_disabled_daily_window_reset() -> None:
    changes = desired_changes(
        list_type="BLOCK",
        enabled=False,
        valid_from=datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
        valid_until=datetime(2026, 10, 5, 12, tzinfo=timezone.utc),
        daily_window_enabled=False,
        daily_start=None,
        daily_end=None,
        camera_comment="Blocked",
        timezone_name="Asia/Kuala_Lumpur",
    )
    assert changes == {
        "need_alarm": 1,
        "enable": 0,
        "vehicle_comment": "Blocked",
        "enable_time": "2026-10-04 20:00:00",
        "overdue_time": "2026-10-05 20:00:00",
        "time_seg_enable": 0,
        "seg_time_start": "00:00:00",
        "seg_time_end": "00:00:00",
    }
    allowed = desired_changes(
        list_type="ALLOW", enabled=True, valid_from=None, valid_until=None,
        daily_window_enabled=True,
        daily_start=datetime.strptime("06:00:00", "%H:%M:%S").time(),
        daily_end=datetime.strptime("23:00:00", "%H:%M:%S").time(),
        camera_comment=None, timezone_name="Asia/Kuala_Lumpur",
    )
    assert allowed["need_alarm"] == 0 and allowed["enable"] == 1
    assert allowed["time_seg_enable"] == 1


def test_query_parsing_merge_and_readback_states() -> None:
    existing = {"plate": "ABC1234", "index": 12, "customer_id": 99, "enable": 1, "need_alarm": 0}
    assert select_record({"payload": {"body": {"state_code": "200", "dldb_rec": [existing]}}}, "ABC1234") == existing
    assert select_record({"payload": {"body": {"state_code": 200, "dldb_rec": None}}}, "ABC1234") is None
    assert select_record({"payload": {"body": {"state_code": 200, "dldb_rec": []}}}, "ABC1234") is None
    for invalid in (
        {},
        {"payload": {"body": {"state_code": 500, "dldb_rec": []}}},
        {"payload": {"body": {"state_code": 200}}},
        {"payload": {"body": {"state_code": 200, "dldb_rec": [{"plate": "OTHER"}]}}},
        {"payload": {"body": {"state_code": 200, "dldb_rec": [existing, existing]}}},
    ):
        with pytest.raises(ValueError):
            select_record(invalid, "ABC1234")

    original = dict(existing)
    merged = merge_record("ABC1234", existing, {"enable": 0, "vehicle_comment": "Visitor"})
    assert merged["index"] == 12 and merged["customer_id"] == 99
    assert merged["enable"] == 0 and existing == original
    assert readback_status({"enable": 1}, {"enable": 1}) == "READBACK_VERIFIED"
    assert readback_status({}, {"enable": 1}) == "READBACK_UNVERIFIED"
    assert readback_status({"enable": 0}, {"enable": 1}) == "READBACK_MISMATCH"


def test_background_sync_queries_merges_updates_and_verifies(db: Session) -> None:
    site, camera = site_and_camera(db)
    camera.connection_state = "ONLINE"
    entry = AccessListService(db).create(plate="ABC1234", site_id=site.id, list_type="ALLOW", enabled=True, camera_comment="Staff")
    db.commit()
    sync = db.scalar(select(AccessListCameraSync))
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    verified_record = {
        "plate": "ABC1234", "index": 7, "need_alarm": 0, "enable": 1,
        "vehicle_comment": "Staff", "time_seg_enable": 0,
        "seg_time_start": "00:00:00", "seg_time_end": "00:00:00",
    }
    manager = FakeCommandManager(factory, [
        {"body": {"state_code": 200, "dldb_rec": None}},
        {"body": {"state_code": 200}},
        {"body": {"state_code": "200", "dldb_rec": [verified_record]}},
    ])
    coordinator = AccessListSyncCoordinator(session_factory=factory, command_manager=manager, is_connected=lambda: True)

    coordinator._process(AccessSyncTask(sync.id, False))
    db.expire_all()
    sync = db.get(AccessListCameraSync, sync.id)
    assert [call[1] for call in manager.calls] == ["ACCESS_QUERY", "ACCESS_UPDATE", "ACCESS_QUERY"]
    assert manager.calls[1][2]["operator_type"] == "update_or_add"
    assert sync.status == "VERIFIED"
    assert sync.remote_index == "7"
    assert sync.last_acknowledged_at and sync.last_verified_at


def test_single_and_delete_all_retain_central_records_and_change_sync_state(db: Session) -> None:
    site, camera = site_and_camera(db)
    camera.connection_state = "ONLINE"
    service = AccessListService(db)
    first = service.create(plate="ABC1234", site_id=site.id, list_type="ALLOW", enabled=True)
    second = service.create(plate="XYZ9876", site_id=site.id, list_type="ALLOW", enabled=True)
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    manager = FakeCommandManager(factory, [{"body": {"state_code": 200}}, {"body": {"state_code": 200}}])
    coordinator = AccessListSyncCoordinator(session_factory=factory, command_manager=manager, is_connected=lambda: True)
    coordinator.delete_camera_access_record(first.id, camera.id)
    db.expire_all()
    first_sync = db.scalar(select(AccessListCameraSync).where(AccessListCameraSync.access_list_entry_id == first.id))
    assert first_sync.status == "NOT_FOUND"
    command, affected = coordinator.delete_all_camera_access_records(camera.id)
    db.expire_all()
    assert affected == 2 and command.status == "CAMERA_ACKNOWLEDGED"
    assert db.get(AccessListEntry, first.id) and db.get(AccessListEntry, second.id)
    assert {row.status for row in db.scalars(select(AccessListCameraSync)).all()} == {"OUT_OF_SYNC"}
    assert manager.calls[0][2] == {"operator_type": "delete", "plate": "ABC1234"}
    assert manager.calls[1][2] == {"operator_type": "delete"}


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"plate": "ABC1234", "need_alarm": 0, "enable": 1}, "PARTIALLY_VERIFIED"),
        ({"plate": "ABC1234", "need_alarm": 1, "enable": 1}, "MISMATCH"),
    ],
)
def test_verify_readback_partial_and_mismatch(db: Session, record: dict, expected: str) -> None:
    site, camera = site_and_camera(db)
    camera.connection_state = "ONLINE"
    entry = AccessListService(db).create(plate="ABC1234", site_id=site.id, list_type="ALLOW", enabled=True)
    db.commit()
    sync = db.scalar(select(AccessListCameraSync))
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    manager = FakeCommandManager(factory, [{"body": {"state_code": 200, "dldb_rec": [record]}}])
    coordinator = AccessListSyncCoordinator(session_factory=factory, command_manager=manager, is_connected=lambda: True)
    coordinator._process(AccessSyncTask(sync.id, True))
    db.expire_all()
    assert db.get(AccessListCameraSync, sync.id).status == expected
    assert len(manager.calls) == 1 and manager.calls[0][1] == "ACCESS_QUERY"


def test_offline_and_uncertain_work_are_not_blindly_replayed(db: Session) -> None:
    site, camera = site_and_camera(db)
    entry = AccessListService(db).create(plate="ABC1234", site_id=site.id, list_type="ALLOW", enabled=True)
    db.commit()
    sync = db.scalar(select(AccessListCameraSync))
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    manager = FakeCommandManager(factory, [])
    coordinator = AccessListSyncCoordinator(session_factory=factory, command_manager=manager, is_connected=lambda: True)
    coordinator._process(AccessSyncTask(sync.id, False))
    db.expire_all()
    sync = db.get(AccessListCameraSync, sync.id)
    assert sync.status == "CAMERA_OFFLINE" and not manager.calls
    sync.status = "TIMEOUT_UNCERTAIN"
    db.commit()
    assert coordinator.enqueue_pending_for_camera(camera.id) == 0


def test_uncertain_manual_reconciliation_queries_before_deciding_to_update(db: Session) -> None:
    site, camera = site_and_camera(db)
    camera.connection_state = "ONLINE"
    AccessListService(db).create(plate="ABC1234", site_id=site.id, list_type="ALLOW", enabled=True)
    db.commit()
    sync = db.scalar(select(AccessListCameraSync))
    sync.status = "TIMEOUT_UNCERTAIN"
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    observed = {
        "plate": "ABC1234", "need_alarm": 0, "enable": 1,
        "vehicle_comment": "", "time_seg_enable": 0,
        "seg_time_start": "00:00:00", "seg_time_end": "00:00:00",
    }
    manager = FakeCommandManager(factory, [{"body": {"state_code": 200, "dldb_rec": [observed]}}])
    coordinator = AccessListSyncCoordinator(session_factory=factory, command_manager=manager, is_connected=lambda: True)

    coordinator._process(AccessSyncTask(sync.id, False))
    db.expire_all()
    assert db.get(AccessListCameraSync, sync.id).status == "VERIFIED"
    assert [call[1] for call in manager.calls] == ["ACCESS_QUERY"]
