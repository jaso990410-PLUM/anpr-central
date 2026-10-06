from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.camera import Camera
from app.models.camera_command import CameraCommand
from app.models.control_board import ControlBoard
from app.integrations.visionzenith.protocol import (
    access_list_envelope,
    device_set_envelope,
    serial_envelope,
)
from app.integrations.visionzenith.topics import (
    access_list_topic,
    device_set_topic,
    serial_command_topic,
)
from app.integrations.visionzenith.access_list import FIELDS
from app.integrations.visionzenith.rs485 import desired_matches, parse_rs485_reply


QUEUED = "QUEUED"
PUBLISHED = "PUBLISHED"
CAMERA_ACKNOWLEDGED = "CAMERA_ACKNOWLEDGED"
CAMERA_REJECTED = "CAMERA_REJECTED"
PUBLISH_FAILED = "PUBLISH_FAILED"
TIMEOUT_UNCERTAIN = "TIMEOUT_UNCERTAIN"
READBACK_VERIFIED = "READBACK_VERIFIED"
READBACK_MISMATCH = "READBACK_MISMATCH"
READBACK_UNVERIFIED = "READBACK_UNVERIFIED"

TERMINAL_STATUSES = {
    CAMERA_ACKNOWLEDGED,
    CAMERA_REJECTED,
    PUBLISH_FAILED,
    TIMEOUT_UNCERTAIN,
    READBACK_VERIFIED,
    READBACK_MISMATCH,
    READBACK_UNVERIFIED,
}

PublishCallable = Callable[[str, str, int], bool]


@dataclass(frozen=True)
class CommandTask:
    camera_id: UUID
    request_id: str
    topic: str
    payload: str
    qos: int


class VisionZenithCommandManager:
    """Durable, per-camera serialisation without automatic action retries."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        publisher: PublishCallable,
        timeout_seconds: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._timeout_seconds = (
            timeout_seconds or settings.visionzenith_command_timeout_seconds
        )
        self._queues: dict[UUID, Queue[CommandTask]] = {}
        self._workers: dict[UUID, Thread] = {}
        self._pending: dict[str, Event] = {}
        self._lock = Lock()
        self._stop = Event()

    def enqueue_serial(
        self,
        *,
        camera_id: UUID,
        serial_number: str,
        command_type: str,
        raw_frame: bytes,
        request_metadata: dict[str, Any] | None = None,
        qos: int = 0,
        serial_channel: int | None = None,
    ) -> CameraCommand:
        request_id = f"{command_type}-{uuid4().hex[:16]}"
        envelope = serial_envelope(
            request_id=request_id,
            serial_number=serial_number,
            bv=settings.visionzenith_bv,
            serial_channel=(
                settings.visionzenith_serial_channel
                if serial_channel is None
                else serial_channel
            ),
            raw_frame=raw_frame,
        )
        return self._persist_and_enqueue(
            camera_id=camera_id,
            request_id=request_id,
            command_type=command_type,
            topic=serial_command_topic(serial_number),
            payload=json.dumps(envelope, ensure_ascii=False),
            qos=qos,
            request_metadata=request_metadata,
        )

    def enqueue_access_list(
        self,
        *,
        camera_id: UUID,
        serial_number: str,
        command_type: str,
        body: dict[str, Any],
        request_metadata: dict[str, Any] | None = None,
    ) -> CameraCommand:
        request_id = f"{command_type}-{uuid4().hex[:16]}"
        envelope = access_list_envelope(
            request_id=request_id,
            serial_number=serial_number,
            bv=settings.visionzenith_bv,
            body=body,
        )
        return self._persist_and_enqueue(
            camera_id=camera_id,
            request_id=request_id,
            command_type=command_type,
            topic=access_list_topic(serial_number),
            payload=json.dumps(envelope, ensure_ascii=False),
            qos=0,
            request_metadata=request_metadata,
        )

    def enqueue_device_setting(
        self,
        *,
        camera_id: UUID,
        serial_number: str,
        command_type: str,
        setting_type: str,
        body: dict[str, Any],
        request_metadata: dict[str, Any] | None = None,
    ) -> CameraCommand:
        request_id = f"{command_type}-{uuid4().hex[:16]}"
        envelope = device_set_envelope(
            request_id=request_id,
            serial_number=serial_number,
            bv=settings.visionzenith_bv,
            setting_type=setting_type,
            body=body,
        )
        return self._persist_and_enqueue(
            camera_id=camera_id,
            request_id=request_id,
            command_type=command_type,
            topic=device_set_topic(serial_number),
            payload=json.dumps(envelope, ensure_ascii=False),
            qos=0,
            request_metadata=request_metadata,
        )

    def _persist_and_enqueue(
        self,
        *,
        camera_id: UUID,
        request_id: str,
        command_type: str,
        topic: str,
        payload: str,
        qos: int,
        request_metadata: dict[str, Any] | None,
    ) -> CameraCommand:
        with self._session_factory() as db:
            command = CameraCommand(
                camera_id=camera_id,
                request_id=request_id,
                command_type=command_type,
                status=QUEUED,
                request_metadata=request_metadata,
            )
            db.add(command)
            db.commit()
            db.refresh(command)
            db.expunge(command)

        task = CommandTask(camera_id, request_id, topic, payload, qos)
        queue = self._camera_queue(camera_id)
        try:
            queue.put_nowait(task)
        except Full:
            self._set_failed(request_id, "Per-camera command queue is full")
        return command

    def _camera_queue(self, camera_id: UUID) -> Queue[CommandTask]:
        with self._lock:
            queue = self._queues.get(camera_id)
            if queue is None:
                queue = Queue(maxsize=100)
                self._queues[camera_id] = queue
                worker = Thread(
                    target=self._camera_worker,
                    args=(camera_id, queue),
                    name=f"anpr-command-{str(camera_id)[:8]}",
                    daemon=True,
                )
                self._workers[camera_id] = worker
                worker.start()
            return queue

    def _camera_worker(
        self, camera_id: UUID, queue: Queue[CommandTask]
    ) -> None:
        while not self._stop.is_set():
            try:
                task = queue.get(timeout=0.25)
            except Empty:
                continue
            event = Event()
            with self._lock:
                self._pending[task.request_id] = event
            try:
                published = self._publisher(task.topic, task.payload, task.qos)
                if not published:
                    self._set_failed(
                        task.request_id,
                        "MQTT publish was not accepted by the client",
                    )
                    continue
                self._set_published(task.request_id)
                if not event.wait(self._timeout_seconds):
                    self._set_timeout(task.request_id)
            except Exception as error:
                self._set_failed(task.request_id, str(error)[:500])
            finally:
                with self._lock:
                    self._pending.pop(task.request_id, None)
                queue.task_done()

    def handle_reply(self, serial_number: str, payload: bytes) -> bool:
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(document, dict) or not isinstance(document.get("id"), str):
            return False
        request_id = document["id"]
        readback: tuple[UUID, str, int] | None = None
        with self._session_factory() as db:
            command = db.scalar(
                select(CameraCommand)
                .join(Camera)
                .where(
                    CameraCommand.request_id == request_id,
                    Camera.serial_number == serial_number,
                )
            )
            if command is None:
                return False
            if command.status in TERMINAL_STATUSES:
                return True
            outer_code = document.get("code")
            body = (document.get("payload") or {}).get("body")
            nested_code = body.get("state_code") if isinstance(body, dict) else None
            accepted = outer_code in (200, "200") and nested_code in (
                None,
                200,
                "200",
            )
            completed_at = datetime.now(timezone.utc)
            command.status = CAMERA_ACKNOWLEDGED if accepted else CAMERA_REJECTED
            if command.published_at is None:
                command.published_at = completed_at
            command.acknowledged_at = completed_at
            command.completed_at = completed_at
            command.camera_result_code = _code_text(outer_code)
            command.nested_result_code = _code_text(nested_code)
            command.response_metadata = {
                "camera_result_code": outer_code,
                "nested_result_code": nested_code,
            }
            if command.command_type.startswith("ACCESS_") and isinstance(body, dict):
                command.response_metadata["access_list_body"] = _safe_access_list_body(body)
            if command.command_type.startswith("RS485_"):
                command.response_metadata["device_set"] = _safe_device_set(document)
                readback = self._apply_rs485_reply(
                    db, command, document, accepted, serial_number
                )
            if not accepted:
                command.error_detail = "Camera returned a non-200 result"
            db.commit()
        with self._lock:
            event = self._pending.get(request_id)
        if event is not None:
            event.set()
        if readback is not None:
            camera_id, camera_serial, source = readback
            self.enqueue_device_setting(
                camera_id=camera_id,
                serial_number=camera_serial,
                command_type="RS485_GET_READBACK",
                setting_type="evs_get_rs485",
                body={"source": source},
                request_metadata={"source": source, "automatic_readback": True},
            )
        return True

    def mark_orphaned_commands(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=self._timeout_seconds
        )
        with self._session_factory() as db:
            commands = db.scalars(
                select(CameraCommand).where(
                    CameraCommand.status.in_((QUEUED, PUBLISHED)),
                    CameraCommand.requested_at <= cutoff,
                )
            ).all()
            for command in commands:
                command.status = TIMEOUT_UNCERTAIN
                command.completed_at = datetime.now(timezone.utc)
                command.error_detail = "No camera acknowledgement before timeout"
            if commands:
                db.commit()
            return len(commands)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            events = tuple(self._pending.values())
            workers = tuple(self._workers.values())
        for event in events:
            event.set()
        for worker in workers:
            worker.join(timeout=2)

    def _set_published(self, request_id: str) -> None:
        with self._session_factory() as db:
            command = db.scalar(
                select(CameraCommand).where(CameraCommand.request_id == request_id)
            )
            if command is not None and command.status == QUEUED:
                command.status = PUBLISHED
                command.published_at = datetime.now(timezone.utc)
                db.commit()

    def _set_failed(self, request_id: str, detail: str) -> None:
        self._set_terminal(request_id, PUBLISH_FAILED, detail)

    def _set_timeout(self, request_id: str) -> None:
        self._set_terminal(
            request_id,
            TIMEOUT_UNCERTAIN,
            "No camera acknowledgement before timeout",
        )

    def _set_terminal(self, request_id: str, status: str, detail: str) -> None:
        with self._session_factory() as db:
            command = db.scalar(
                select(CameraCommand).where(CameraCommand.request_id == request_id)
            )
            if command is not None and command.status not in TERMINAL_STATUSES:
                command.status = status
                command.completed_at = datetime.now(timezone.utc)
                command.error_detail = detail
                if command.command_type.startswith("RS485_"):
                    board = db.scalar(
                        select(ControlBoard).where(
                            ControlBoard.camera_id == command.camera_id
                        )
                    )
                    if board is not None:
                        board.communication_sync_status = (
                            "TIMEOUT_UNCERTAIN"
                            if status == TIMEOUT_UNCERTAIN
                            else "ERROR"
                        )
                        board.last_error = detail
                db.commit()

    @staticmethod
    def _apply_rs485_reply(
        db: Session,
        command: CameraCommand,
        document: dict[str, Any],
        accepted: bool,
        serial_number: str,
    ) -> tuple[UUID, str, int] | None:
        board = db.scalar(
            select(ControlBoard).where(ControlBoard.camera_id == command.camera_id)
        )
        if board is None:
            return None
        if not accepted:
            board.communication_sync_status = "ERROR"
            board.last_error = "Camera rejected the RS-485 request"
            return None
        if command.command_type == "RS485_SET":
            board.communication_sync_status = "APPLIED_UNVERIFIED"
            board.last_error = None
            return command.camera_id, serial_number, board.serial_channel
        try:
            observed = parse_rs485_reply(document)
        except ValueError as error:
            board.communication_sync_status = "ERROR"
            board.last_error = str(error)
            return None
        board.observed_serial_channel = observed.source
        board.observed_baud_rate = observed.baud_rate
        board.observed_data_bits = observed.data_bits
        board.observed_parity_id = observed.parity_id
        board.observed_stop_bits = observed.stop_bits
        board.last_communication_read_at = datetime.now(timezone.utc)
        matches = desired_matches(
            observed,
            source=board.serial_channel,
            baud_rate=board.baud_rate,
            data_bits=board.data_bits,
            parity=board.parity,
            stop_bits=board.stop_bits,
        )
        board.communication_sync_status = "IN_SYNC" if matches else "MISMATCH"
        board.last_error = None if matches else "Observed camera RS-485 settings differ from Central desired state"
        return None


def _code_text(value: Any) -> str | None:
    return None if value is None else str(value)[:32]


def _safe_access_list_body(body: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key in ("state_code", "operator_type"):
        if key in body and isinstance(body[key], (str, int)):
            safe[key] = body[key]
    records = body.get("dldb_rec")
    if records is None and "dldb_rec" in body:
        safe["dldb_rec"] = None
    elif isinstance(records, list):
        safe["dldb_rec"] = [
            {key: value for key, value in record.items() if key in FIELDS and isinstance(value, (str, int, float, bool, type(None)))}
            for record in records
            if isinstance(record, dict)
        ][:10]
    return safe


def _safe_device_set(document: dict[str, Any]) -> dict[str, Any]:
    payload = document.get("payload")
    if not isinstance(payload, dict):
        return {}
    safe: dict[str, Any] = {}
    if payload.get("type") in {"evs_get_rs485", "evs_set_rs485"}:
        safe["type"] = payload["type"]
    body = payload.get("body")
    if isinstance(body, dict):
        safe["body"] = {
            key: value
            for key, value in body.items()
            if key in {"source", "baud_rate", "data_bits", "parity_id", "stop_bits"}
            and isinstance(value, int)
        }
    return safe
