import json
import time

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.integrations.visionzenith.command_manager import (
    CAMERA_ACKNOWLEDGED,
    CAMERA_REJECTED,
    PUBLISHED,
    QUEUED,
    TIMEOUT_UNCERTAIN,
    VisionZenithCommandManager,
)
from app.integrations.visionzenith.protocol import led_frame
from app.models.camera import Camera
from app.models.camera_command import CameraCommand
from app.models.control_board import ControlBoard
from app.models.site import Site


def camera(db: Session, *, code: str, serial: str) -> Camera:
    site = db.scalar(select(Site).where(Site.code == "MAIN"))
    if site is None:
        site = Site(
            code="MAIN",
            name="Main Site",
            timezone="Asia/Kuala_Lumpur",
            is_active=True,
        )
        db.add(site)
        db.flush()
    value = Camera(
        site=site,
        code=code,
        name=code,
        direction="ENTRY",
        serial_number=serial,
    )
    db.add(value)
    db.commit()
    return value


def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Condition was not reached before timeout")


def command_status(factory, request_id: str) -> str | None:
    with factory() as session:
        return session.scalar(
            select(CameraCommand.status).where(
                CameraCommand.request_id == request_id
            )
        )


@pytest.mark.parametrize("result_code", [200, "200"])
def test_command_registered_before_publish_and_reply_correlated(
    db: Session, result_code: int | str
) -> None:
    target = camera(db, code="CAM-1", serial="serial-1")
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    observed_statuses: list[str | None] = []

    def publish(_topic: str, payload: str, _qos: int) -> bool:
        request_id = json.loads(payload)["id"]
        observed_statuses.append(command_status(factory, request_id))
        return True

    manager = VisionZenithCommandManager(
        session_factory=factory,
        publisher=publish,
        timeout_seconds=1,
    )
    try:
        command = manager.enqueue_serial(
            camera_id=target.id,
            serial_number=target.serial_number,
            command_type="display_plate",
            raw_frame=led_frame("ABC123"),
        )
        wait_until(lambda: command_status(factory, command.request_id) == PUBLISHED)
        assert observed_statuses == [QUEUED]

        handled = manager.handle_reply(
            target.serial_number,
            json.dumps({"id": command.request_id, "code": result_code}).encode(),
        )
        wait_until(
            lambda: command_status(factory, command.request_id)
            == CAMERA_ACKNOWLEDGED
        )
        assert handled
        assert manager.handle_reply(
            target.serial_number,
            json.dumps({"id": command.request_id, "code": result_code}).encode(),
        )
    finally:
        manager.stop()


def test_unknown_and_malformed_replies_are_safe(db: Session) -> None:
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    manager = VisionZenithCommandManager(
        session_factory=factory,
        publisher=lambda *_args: True,
        timeout_seconds=5,
    )
    try:
        assert not manager.handle_reply("serial-1", b"not-json")
        assert not manager.handle_reply(
            "serial-1", b'{"id":"unknown","code":200}'
        )
    finally:
        manager.stop()


def test_access_list_reply_accepts_string_codes_and_stores_only_safe_readback(
    db: Session,
) -> None:
    target = camera(db, code="CAM-1", serial="serial-1")
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    manager = VisionZenithCommandManager(
        session_factory=factory,
        publisher=lambda *_args: True,
        timeout_seconds=10,
    )
    try:
        command = manager.enqueue_access_list(
            camera_id=target.id,
            serial_number=target.serial_number,
            command_type="ACCESS_QUERY",
            body={"operator_type": "select", "plate": "ABC1234", "sub_type": "plate"},
            request_metadata={"plate": "ABC1234"},
        )
        wait_until(lambda: command_status(factory, command.request_id) == PUBLISHED)
        assert manager.handle_reply(
            target.serial_number,
            json.dumps({
                "id": command.request_id,
                "code": "200",
                "payload": {"body": {"state_code": "200", "operator_type": "select", "dldb_rec": [{"plate": "ABC1234", "enable": 1, "need_alarm": 0, "password": "excluded"}]}},
            }).encode(),
        )
        wait_until(lambda: command_status(factory, command.request_id) == CAMERA_ACKNOWLEDGED)
        with factory() as session:
            stored = session.scalar(select(CameraCommand).where(CameraCommand.id == command.id))
            assert stored.response_metadata["access_list_body"]["dldb_rec"] == [
                {"plate": "ABC1234", "enable": 1, "need_alarm": 0}
            ]
            assert "password" not in json.dumps(stored.response_metadata)
    finally:
        manager.stop()


def test_non_200_or_nested_failure_rejects_command(db: Session) -> None:
    target = camera(db, code="CAM-1", serial="serial-1")
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    manager = VisionZenithCommandManager(
        session_factory=factory,
        publisher=lambda *_args: True,
        timeout_seconds=5,
    )
    try:
        command = manager.enqueue_serial(
            camera_id=target.id,
            serial_number=target.serial_number,
            command_type="voice",
            raw_frame=b"voice",
        )
        wait_until(lambda: command_status(factory, command.request_id) == PUBLISHED)
        assert manager.handle_reply(
            target.serial_number,
            json.dumps(
                {
                    "id": command.request_id,
                    "code": 200,
                    "payload": {"body": {"state_code": 500}},
                }
            ).encode(),
        )
        wait_until(
            lambda: command_status(factory, command.request_id) == CAMERA_REJECTED
        )
    finally:
        manager.stop()


def test_rs485_get_reply_updates_observed_state_and_compares_desired(db: Session) -> None:
    target = camera(db, code="CAM-RS485", serial="serial-rs485")
    board = ControlBoard(camera=target, serial_channel=0, baud_rate=9600, data_bits=8, parity="NONE", stop_bits=1, slave_id=1)
    db.add(board); db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    manager = VisionZenithCommandManager(session_factory=factory, publisher=lambda *_args: True, timeout_seconds=10)
    try:
        command = CameraCommand(camera_id=target.id, request_id="rs485-get-test", command_type="RS485_GET", status=QUEUED, request_metadata={"source": 0})
        db.add(command); db.commit()
        assert manager.handle_reply(target.serial_number, json.dumps({"id": command.request_id, "code": 200, "payload": {"type": "evs_get_rs485", "body": {"source": 0, "baud_rate": 9600, "data_bits": 8, "parity_id": 0, "stop_bits": 1}}}).encode())
        with factory() as session:
            stored = session.get(ControlBoard, board.id)
            assert stored.communication_sync_status == "IN_SYNC"
            assert stored.observed_baud_rate == 9600 and stored.observed_parity_id == 0
            assert stored.last_communication_read_at is not None
    finally:
        manager.stop()


def test_timeout_is_uncertain_and_action_is_not_retried(db: Session) -> None:
    target = camera(db, code="CAM-1", serial="serial-1")
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    published: list[str] = []

    def publish(_topic: str, payload: str, _qos: int) -> bool:
        published.append(json.loads(payload)["id"])
        return True

    manager = VisionZenithCommandManager(
        session_factory=factory,
        publisher=publish,
        timeout_seconds=0.1,
    )
    try:
        command = manager.enqueue_serial(
            camera_id=target.id,
            serial_number=target.serial_number,
            command_type="traffic_green",
            raw_frame=b"green",
        )
        wait_until(
            lambda: command_status(factory, command.request_id)
            == TIMEOUT_UNCERTAIN
        )
        time.sleep(0.15)
        assert published == [command.request_id]
    finally:
        manager.stop()


def test_same_camera_is_serialized_but_different_cameras_are_independent(
    db: Session,
) -> None:
    first_camera = camera(db, code="CAM-1", serial="serial-1")
    second_camera = camera(db, code="CAM-2", serial="serial-2")
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    published: list[str] = []

    def publish(_topic: str, payload: str, _qos: int) -> bool:
        published.append(json.loads(payload)["id"])
        return True

    manager = VisionZenithCommandManager(
        session_factory=factory,
        publisher=publish,
        timeout_seconds=1,
    )
    try:
        first = manager.enqueue_serial(
            camera_id=first_camera.id,
            serial_number=first_camera.serial_number,
            command_type="display_plate",
            raw_frame=b"one",
        )
        queued_same_camera = manager.enqueue_serial(
            camera_id=first_camera.id,
            serial_number=first_camera.serial_number,
            command_type="voice",
            raw_frame=b"two",
        )
        independent = manager.enqueue_serial(
            camera_id=second_camera.id,
            serial_number=second_camera.serial_number,
            command_type="display_plate",
            raw_frame=b"three",
        )
        wait_until(lambda: len(published) == 2)
        assert first.request_id in published
        assert independent.request_id in published
        assert queued_same_camera.request_id not in published

        manager.handle_reply(
            first_camera.serial_number,
            json.dumps({"id": first.request_id, "code": 200}).encode(),
        )
        wait_until(lambda: queued_same_camera.request_id in published)
    finally:
        manager.stop()
