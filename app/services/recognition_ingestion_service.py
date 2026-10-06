from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.integrations.visionzenith.command_manager import VisionZenithCommandManager
from app.integrations.visionzenith.protocol import led_frame
from app.integrations.visionzenith.recognition import (
    RecognitionMessage,
    RecognitionRejected,
    image_candidates,
    parse_recognition,
)
from app.integrations.visionzenith.topics import OFFLINE_RECOGNITION, QUICK_RECOGNITION, RECOGNITION
from app.models.camera import Camera
from app.models.control_board import ControlBoard
from app.models.recognition_event import RecognitionEvent
from app.services.camera_service import CREDENTIALS_REVOKED
from app.services.recognition_media_service import (
    RecognitionMediaError,
    RecognitionMediaService,
)


logger = logging.getLogger(__name__)
RECOGNITION_QUEUE_SIZE = 128


class RecognitionIngestionRejected(ValueError):
    pass


class RecognitionIngestionService:
    def __init__(
        self,
        db: Session,
        *,
        media: RecognitionMediaService | None = None,
    ) -> None:
        self.db = db
        self.media = media or RecognitionMediaService()

    def process(
        self,
        *,
        serial_number: str,
        message_type: str,
        payload: bytes,
        retained: bool,
        received_at: datetime | None = None,
    ) -> tuple[RecognitionEvent, bool, bool]:
        if message_type not in {QUICK_RECOGNITION, RECOGNITION, OFFLINE_RECOGNITION}:
            raise RecognitionIngestionRejected("Unsupported recognition message type")
        camera = self.db.scalar(
            select(Camera).where(Camera.serial_number == serial_number)
        )
        if camera is None:
            raise RecognitionIngestionRejected("Unknown camera serial number")
        if not camera.is_enabled:
            raise RecognitionIngestionRejected("Camera is disabled")
        if camera.provisioning_state == CREDENTIALS_REVOKED:
            raise RecognitionIngestionRejected("Camera provisioning is revoked")
        try:
            parsed = parse_recognition(payload, retained=retained)
        except RecognitionRejected as error:
            raise RecognitionIngestionRejected(str(error)) from error
        received = _as_utc(received_at or datetime.now(timezone.utc))
        historical = message_type == OFFLINE_RECOGNITION
        # PlateResult.direction is vendor motion (unknown/left/right/up/down),
        # not the operational lane direction. Entry/exit belongs to the camera.
        direction = _configured_direction(camera.direction)
        event = self._correlate(
            camera=camera,
            parsed=parsed,
            direction=direction,
            received_at=received,
            historical=historical,
        )
        created = event is None
        if event is None:
            event = RecognitionEvent(
                camera_id=camera.id,
                site_id=camera.site_id,
                vendor_event_id=parsed.event_id,
                vendor_record_id=parsed.record_id,
                plate_text=parsed.plate,
                direction=direction,
                confidence=parsed.confidence,
                captured_at=parsed.captured_at,
                received_at=received,
                event_type=("QUICK" if message_type == QUICK_RECOGNITION else "FULL"),
                source="OFFLINE_RECOVERY" if historical else "LIVE",
                recovered_at=received if historical else None,
                event_metadata=parsed.metadata or None,
            )
            self.db.add(event)
            self.db.flush()
        elif message_type in {RECOGNITION, OFFLINE_RECOGNITION}:
            event.event_type = "ENRICHED"
            if parsed.confidence is not None:
                event.confidence = parsed.confidence
            event.captured_at = parsed.captured_at or event.captured_at
            event.direction = direction or event.direction
            event.vendor_record_id = event.vendor_record_id or parsed.record_id
            event.event_metadata = {
                **(event.event_metadata or {}),
                **parsed.metadata,
            } or None
            if historical:
                event.recovered_at = received
                event.event_metadata = {**(event.event_metadata or {}), "offline_recovery_duplicate": True}

        self._store_images(event, parsed)
        self.db.flush()

        should_display = (
            created
            and message_type == QUICK_RECOGNITION
            and settings.visionzenith_auto_led_enabled
            and _is_current_capture(parsed.captured_at, received)
        )
        return event, created, should_display

    def _correlate(
        self,
        *,
        camera: Camera,
        parsed: RecognitionMessage,
        direction: str | None,
        received_at: datetime,
        historical: bool,
    ) -> RecognitionEvent | None:
        cutoff = received_at - timedelta(
            seconds=settings.recognition_correlation_seconds
        )
        common = (
            RecognitionEvent.camera_id == camera.id,
            RecognitionEvent.plate_text == parsed.plate,
        )
        if parsed.event_id:
            matches = self.db.scalars(
                select(RecognitionEvent)
                .where(
                    *common, RecognitionEvent.vendor_event_id == parsed.event_id
                )
                .order_by(RecognitionEvent.received_at.desc())
                .limit(2)
            ).all()
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                return None
        if parsed.record_id:
            matches = self.db.scalars(
                select(RecognitionEvent).where(
                    *common, RecognitionEvent.vendor_record_id == parsed.record_id
                ).limit(2)
            ).all()
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                return None
        direction_clause = (
            RecognitionEvent.direction.is_(None)
            if direction is None
            else RecognitionEvent.direction == direction
        )
        if historical and parsed.captured_at is not None:
            capture = _as_utc(parsed.captured_at)
            matches = self.db.scalars(select(RecognitionEvent).where(
                *common,
                RecognitionEvent.captured_at >= capture - timedelta(seconds=settings.recognition_correlation_seconds),
                RecognitionEvent.captured_at <= capture + timedelta(seconds=settings.recognition_correlation_seconds),
                direction_clause,
            )).all()
        elif not historical:
            matches = self.db.scalars(select(RecognitionEvent).where(
                *common,
                RecognitionEvent.received_at >= cutoff,
                RecognitionEvent.received_at <= received_at + timedelta(seconds=1),
                direction_clause,
            )).all()
        else:
            matches = []
        return matches[0] if len(matches) == 1 else None

    def _store_images(
        self, event: RecognitionEvent, parsed: RecognitionMessage
    ) -> None:
        candidates = image_candidates(parsed.plate_result)
        selected: dict[str, str] = {}
        for candidate_kind, encoded in candidates:
            kind = "scene" if candidate_kind == "scene" else "plate"
            selected.setdefault(kind, encoded)
        for kind, encoded in selected.items():
            attribute = f"{kind}_image_path"
            if getattr(event, attribute):
                continue
            try:
                setattr(event, attribute, self.media.store(event.id, kind, encoded))
            except RecognitionMediaError:
                logger.warning(
                    "Rejected invalid %s image for recognition event %s",
                    kind,
                    event.id,
                )


@dataclass(frozen=True)
class RecognitionTask:
    serial_number: str
    message_type: str
    payload: bytes
    retained: bool
    received_at: datetime


class RecognitionWorker:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        command_manager: VisionZenithCommandManager,
        peripheral_event_callback: Callable[[UUID, str], None] | None = None,
        recovery_result_callback: Callable[[str, bool, bool], None] | None = None,
        queue_size: int = RECOGNITION_QUEUE_SIZE,
    ) -> None:
        self._session_factory = session_factory
        self._command_manager = command_manager
        self._peripheral_event_callback = peripheral_event_callback
        self._recovery_result_callback = recovery_result_callback
        self._queue: Queue[RecognitionTask] = Queue(maxsize=queue_size)
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(
            target=self._run,
            name="anpr-recognition-ingestion",
            daemon=True,
        )
        self._thread.start()

    def enqueue(
        self,
        serial_number: str,
        message_type: str,
        payload: bytes,
        *,
        retained: bool,
    ) -> bool:
        if retained:
            logger.warning("Retained recognition message rejected")
            return False
        try:
            self._queue.put_nowait(
                RecognitionTask(
                    serial_number,
                    message_type,
                    bytes(payload),
                    retained,
                    datetime.now(timezone.utc),
                )
            )
            return True
        except Full:
            logger.warning("Recognition queue is full; event dropped")
            return False

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def drain(self) -> None:
        """Wait until events already handed to the ingestion worker are durable."""
        self._queue.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._queue.get(timeout=0.25)
            except Empty:
                continue
            try:
                with self._session_factory() as db:
                    try:
                        event, _created, should_display = RecognitionIngestionService(db).process(
                            serial_number=task.serial_number,
                            message_type=task.message_type,
                            payload=task.payload,
                            retained=task.retained,
                            received_at=task.received_at,
                        )
                        board = db.scalar(
                            select(ControlBoard).where(
                                ControlBoard.camera_id == event.camera_id,
                                ControlBoard.enabled.is_(True),
                            )
                        )
                        action = (
                            event.camera_id,
                            event.plate_text,
                            event.vendor_event_id,
                            event.id,
                            board.serial_channel if board else None,
                            board.slave_id if board else 1,
                        )
                        db.commit()
                        if task.message_type == OFFLINE_RECOGNITION and self._recovery_result_callback is not None:
                            self._recovery_result_callback(task.serial_number, _created, True)
                        if should_display:
                            self._command_manager.enqueue_serial(
                                camera_id=action[0],
                                serial_number=task.serial_number,
                                command_type="display_plate",
                                raw_frame=led_frame(action[1], address=action[5]),
                                request_metadata={
                                    "plate": action[1],
                                    "event_id": action[2],
                                    "recognition_event_id": str(action[3]),
                                },
                                serial_channel=action[4],
                            )
                            if board is not None and self._peripheral_event_callback is not None:
                                self._peripheral_event_callback(action[0], action[1])
                    except RecognitionIngestionRejected as error:
                        db.rollback()
                        if task.message_type == OFFLINE_RECOGNITION and self._recovery_result_callback is not None:
                            self._recovery_result_callback(task.serial_number, False, False)
                        logger.warning("Recognition rejected: %s", error)
                    except Exception:
                        db.rollback()
                        logger.exception("Recognition ingestion failed")
            finally:
                self._queue.task_done()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_current_capture(
    captured_at: datetime | None, received_at: datetime
) -> bool:
    if captured_at is None:
        return True
    difference = abs((received_at - _as_utc(captured_at)).total_seconds())
    return difference <= settings.recognition_correlation_seconds


def _configured_direction(value: str | None) -> str:
    clean = (value or "").strip().upper()
    return clean[:20] if clean else "UNKNOWN"
