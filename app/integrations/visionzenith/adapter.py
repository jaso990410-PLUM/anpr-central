from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
import json
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.visionzenith.topics import KEEP_ALIVE, parse_native_topic
from app.integrations.visionzenith.command_manager import VisionZenithCommandManager
from app.integrations.visionzenith.protocol import led_frame
from app.integrations.visionzenith.recognition import (
    RecognitionRejected,
    parse_recognition,
)
from app.integrations.visionzenith.topics import (
    ACCESS_LIST_REPLY,
    DEVICE_SET_REPLY,
    OFFLINE_RECOVERY_REPLY,
    QUICK_RECOGNITION,
    RECOGNITION,
    SERIAL_REPLY,
)
from app.models.camera import Camera
from app.services.camera_presence_service import mark_camera_online
from app.services.camera_service import CREDENTIALS_REVOKED


class NativeMessageRejected(ValueError):
    pass


@dataclass(frozen=True)
class NativeHeartbeat:
    serial_number: str


def parse_native_heartbeat(topic: str, payload: bytes) -> NativeHeartbeat:
    parsed_topic = parse_native_topic(topic)
    if parsed_topic is None or parsed_topic.message_type != KEEP_ALIVE:
        raise NativeMessageRejected("Invalid VisionZenith heartbeat topic")

    if payload.strip():
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            document = None
        if isinstance(document, dict) and "sn" in document:
            payload_serial = document.get("sn")
            if str(payload_serial) != parsed_topic.serial_number:
                raise NativeMessageRejected(
                    "VisionZenith topic and payload serial do not match"
                )
    return NativeHeartbeat(serial_number=parsed_topic.serial_number)


def process_native_heartbeat(
    db: Session,
    topic: str,
    payload: bytes,
    *,
    now: datetime | None = None,
) -> Camera:
    heartbeat = parse_native_heartbeat(topic, payload)
    camera = db.scalar(
        select(Camera).where(Camera.serial_number == heartbeat.serial_number)
    )
    if camera is None:
        raise NativeMessageRejected("Unknown VisionZenith camera serial number")
    if not camera.is_enabled:
        raise NativeMessageRejected("Camera is disabled")
    if camera.provisioning_state == CREDENTIALS_REVOKED:
        raise NativeMessageRejected("Camera provisioning is revoked")
    return mark_camera_online(db, camera, now=now)


class VisionZenithAdapter:
    def __init__(self, *, auto_led_enabled: bool = True) -> None:
        self._auto_led_enabled = auto_led_enabled
        self._seen_events: OrderedDict[str, bool] = OrderedDict()
        self._last_plate: dict[str, tuple[str, float]] = {}

    def dispatch(
        self,
        db: Session,
        topic: str,
        payload: bytes,
        *,
        retained: bool,
        command_manager: VisionZenithCommandManager,
        now: datetime | None = None,
    ) -> str:
        native_topic = parse_native_topic(topic)
        if native_topic is None:
            raise NativeMessageRejected("Unsupported VisionZenith topic")
        if native_topic.message_type == KEEP_ALIVE:
            process_native_heartbeat(db, topic, payload, now=now)
            return KEEP_ALIVE
        if native_topic.message_type in (SERIAL_REPLY, ACCESS_LIST_REPLY, DEVICE_SET_REPLY, OFFLINE_RECOVERY_REPLY):
            if not command_manager.handle_reply(
                native_topic.serial_number, payload
            ):
                raise NativeMessageRejected("Unknown or malformed command reply")
            return native_topic.message_type
        if native_topic.message_type in (QUICK_RECOGNITION, RECOGNITION):
            self._handle_recognition(
                db,
                native_topic.serial_number,
                payload,
                retained=retained,
                command_manager=command_manager,
            )
            return native_topic.message_type
        raise NativeMessageRejected("Unsupported VisionZenith message type")

    def _handle_recognition(
        self,
        db: Session,
        serial_number: str,
        payload: bytes,
        *,
        retained: bool,
        command_manager: VisionZenithCommandManager,
    ) -> None:
        try:
            recognition = parse_recognition(payload, retained=retained)
        except RecognitionRejected as error:
            raise NativeMessageRejected(str(error)) from error
        camera = db.scalar(
            select(Camera).where(Camera.serial_number == serial_number)
        )
        if camera is None:
            raise NativeMessageRejected("Unknown VisionZenith camera serial number")
        if not camera.is_enabled:
            raise NativeMessageRejected("Camera is disabled")
        if camera.provisioning_state == CREDENTIALS_REVOKED:
            raise NativeMessageRejected("Camera provisioning is revoked")
        if not self._auto_led_enabled:
            return
        event_key = (
            f"{serial_number}:{recognition.event_id}"
            if recognition.event_id
            else None
        )
        if event_key and event_key in self._seen_events:
            return
        monotonic_now = time.monotonic()
        previous = self._last_plate.get(serial_number)
        if (
            previous is not None
            and previous[0] == recognition.plate
            and monotonic_now - previous[1] < 3
        ):
            return
        command_manager.enqueue_serial(
            camera_id=camera.id,
            serial_number=serial_number,
            command_type="display_plate",
            raw_frame=led_frame(recognition.plate),
            request_metadata={
                "plate": recognition.plate,
                "event_id": recognition.event_id,
            },
        )
        self._last_plate[serial_number] = (recognition.plate, monotonic_now)
        if event_key:
            self._seen_events[event_key] = True
            if len(self._seen_events) > 500:
                self._seen_events.popitem(last=False)
