from dataclasses import dataclass
from datetime import datetime, timezone
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
import time
from typing import Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.integrations.visionzenith.access_list import (
    delete_all_camera_access_records as delete_all_request,
    delete_camera_access_record as delete_one_request,
    desired_changes,
    merge_record,
    select_record,
    verify_changes,
)
from app.integrations.visionzenith.command_manager import (
    CAMERA_ACKNOWLEDGED,
    CAMERA_REJECTED,
    PUBLISH_FAILED,
    TIMEOUT_UNCERTAIN,
    VisionZenithCommandManager,
)
from app.models.access_list_camera_sync import AccessListCameraSync
from app.models.access_list_entry import AccessListEntry
from app.models.camera import Camera
from app.models.camera_command import CameraCommand
from app.services.camera_service import ONLINE


@dataclass(frozen=True)
class AccessSyncTask:
    sync_id: UUID
    verify_only: bool


class AccessListSyncCoordinator:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        command_manager: VisionZenithCommandManager,
        is_connected: Callable[[], bool],
        worker_count: int = 4,
    ) -> None:
        self._session_factory = session_factory
        self._command_manager = command_manager
        self._is_connected = is_connected
        self._queue: Queue[AccessSyncTask] = Queue(maxsize=500)
        self._stop = Event()
        self._workers: list[Thread] = []
        self._inflight: set[UUID] = set()
        self._lock = Lock()
        self._worker_count = worker_count

    def start(self) -> None:
        if self._workers:
            return
        self._stop.clear()
        for number in range(self._worker_count):
            worker = Thread(target=self._run, name=f"anpr-access-sync-{number + 1}", daemon=True)
            self._workers.append(worker)
            worker.start()

    def stop(self) -> None:
        self._stop.set()
        for worker in self._workers:
            worker.join(timeout=2)
        self._workers.clear()

    def enqueue_entry(self, entry_id: UUID, *, verify_only: bool = False) -> int:
        with self._session_factory() as db:
            sync_ids = tuple(
                db.scalars(
                    select(AccessListCameraSync.id).where(
                        AccessListCameraSync.access_list_entry_id == entry_id
                    )
                ).all()
            )
        return sum(self.enqueue_sync(sync_id, verify_only=verify_only) for sync_id in sync_ids)

    def enqueue_pending_for_camera(self, camera_id: UUID) -> int:
        with self._session_factory() as db:
            sync_ids = tuple(
                db.scalars(
                    select(AccessListCameraSync.id).where(
                        AccessListCameraSync.camera_id == camera_id,
                        AccessListCameraSync.status.in_(("PENDING", "CAMERA_OFFLINE")),
                    )
                ).all()
            )
        return sum(self.enqueue_sync(sync_id) for sync_id in sync_ids)

    def enqueue_sync(self, sync_id: UUID, *, verify_only: bool = False) -> bool:
        with self._lock:
            if sync_id in self._inflight:
                return False
            self._inflight.add(sync_id)
        try:
            self._queue.put_nowait(AccessSyncTask(sync_id, verify_only))
            return True
        except Full:
            with self._lock:
                self._inflight.discard(sync_id)
            return False

    def delete_camera_access_record(self, entry_id: UUID, camera_id: UUID) -> CameraCommand:
        with self._session_factory() as db:
            sync = db.scalar(
                select(AccessListCameraSync)
                .options(selectinload(AccessListCameraSync.camera), selectinload(AccessListCameraSync.access_entry).selectinload(AccessListEntry.vehicle))
                .where(AccessListCameraSync.access_list_entry_id == entry_id, AccessListCameraSync.camera_id == camera_id)
            )
            if sync is None:
                raise RuntimeError("Camera synchronization record not found")
            self._require_usable(sync.camera)
            plate = sync.access_entry.vehicle.plate_text
            command = self._execute(sync.camera, "ACCESS_DELETE", delete_one_request(plate), metadata={"plate": plate})
            if command.status != CAMERA_ACKNOWLEDGED:
                raise RuntimeError(command.error_detail or "Camera delete was not acknowledged")
            sync.status = "NOT_FOUND"
            sync.remote_index = None
            sync.last_acknowledged_at = command.acknowledged_at or datetime.now(timezone.utc)
            sync.last_verified_at = sync.last_acknowledged_at
            sync.last_error = "Explicitly deleted from this camera; Central desired record retained"
            sync.verification_details = {"camera_record_deleted": True}
            db.commit()
            return command

    def delete_all_camera_access_records(self, camera_id: UUID) -> tuple[CameraCommand, int]:
        with self._session_factory() as db:
            camera = db.get(Camera, camera_id)
            if camera is None:
                raise RuntimeError("Camera not found")
            self._require_usable(camera)
            command = self._execute(camera, "ACCESS_DELETE_ALL", delete_all_request(), metadata={"delete_all": True})
            if command.status != CAMERA_ACKNOWLEDGED:
                raise RuntimeError(command.error_detail or "Camera delete-all was not acknowledged")
            syncs = db.scalars(select(AccessListCameraSync).where(AccessListCameraSync.camera_id == camera_id)).all()
            now = command.acknowledged_at or datetime.now(timezone.utc)
            for sync in syncs:
                sync.status = "OUT_OF_SYNC"
                sync.remote_index = None
                sync.last_acknowledged_at = now
                sync.last_verified_at = None
                sync.last_error = "Camera list was deliberately cleared; Central desired record retained"
                sync.verification_details = {"camera_delete_all": True}
            db.commit()
            return command, len(syncs)

    def _require_usable(self, camera: Camera) -> None:
        if not self._is_connected():
            raise RuntimeError("MQTT broker is not connected")
        if not camera.is_enabled or camera.connection_state != ONLINE:
            raise RuntimeError("Camera is not online")
        if not camera.serial_number:
            raise RuntimeError("Camera serial number is required")
        if not camera.access_list_sync_enabled:
            raise RuntimeError("Camera access-list synchronization is disabled")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._queue.get(timeout=0.25)
            except Empty:
                continue
            try:
                self._process(task)
            finally:
                with self._lock:
                    self._inflight.discard(task.sync_id)
                self._queue.task_done()

    def _process(self, task: AccessSyncTask) -> None:
        with self._session_factory() as db:
            sync = db.scalar(
                select(AccessListCameraSync)
                .options(
                    selectinload(AccessListCameraSync.camera),
                    selectinload(AccessListCameraSync.access_entry).selectinload(AccessListEntry.vehicle),
                    selectinload(AccessListCameraSync.access_entry).selectinload(AccessListEntry.site),
                )
                .where(AccessListCameraSync.id == task.sync_id)
            )
            if sync is None:
                return
            camera = sync.camera
            if (
                not self._is_connected()
                or not camera.is_enabled
                or camera.connection_state != ONLINE
                or not camera.serial_number
                or not camera.access_list_sync_enabled
            ):
                sync.status = "CAMERA_OFFLINE"
                sync.last_error = "Camera or broker is not currently online"
                db.commit()
                return
            sync.status = "SYNCING"
            sync.last_attempt_at = datetime.now(timezone.utc)
            sync.last_error = None
            db.commit()

            entry = sync.access_entry
            changes = desired_changes(
                list_type=entry.list_type,
                enabled=entry.enabled,
                valid_from=entry.valid_from,
                valid_until=entry.valid_until,
                daily_window_enabled=entry.daily_window_enabled,
                daily_start=entry.daily_start,
                daily_end=entry.daily_end,
                camera_comment=entry.camera_comment,
                timezone_name=entry.site.timezone,
            )
            plate = entry.vehicle.plate_text
            try:
                existing = self._query(camera, plate)
                if task.verify_only:
                    self._apply_verification(sync, existing, changes)
                    db.commit()
                    return
                if existing is not None:
                    _missing, mismatches = verify_changes(existing, changes)
                    if not mismatches:
                        # The observed record already agrees with every field the
                        # camera returned. Omitted fields remain unverified; do not
                        # replay an uncertain update merely to chase an omission.
                        self._apply_verification(sync, existing, changes)
                        db.commit()
                        return
                merged = merge_record(plate, existing, changes)
                update = self._execute(
                    camera,
                    "ACCESS_UPDATE",
                    {"operator_type": "update_or_add", "dldb_rec": merged},
                    plate,
                )
                if update.status != CAMERA_ACKNOWLEDGED:
                    self._command_failure(sync, update)
                    db.commit()
                    return
                sync.status = "ACKNOWLEDGED"
                sync.last_acknowledged_at = update.acknowledged_at or datetime.now(timezone.utc)
                db.commit()
                confirmed = self._query(camera, plate)
                if confirmed is None:
                    sync.status = "NOT_FOUND"
                    sync.last_error = "Update acknowledged but the plate was absent on readback"
                else:
                    self._apply_verification(sync, confirmed, changes)
            except TimeoutError as error:
                sync.status = "TIMEOUT_UNCERTAIN"
                sync.last_error = str(error)[:500]
            except (ValueError, RuntimeError) as error:
                sync.status = "ERROR"
                sync.last_error = str(error)[:500]
            db.commit()

    def _query(self, camera: Camera, plate: str) -> dict | None:
        command = self._execute(
            camera,
            "ACCESS_QUERY",
            {"operator_type": "select", "plate": plate, "sub_type": "plate"},
            plate,
        )
        if command.status != CAMERA_ACKNOWLEDGED:
            if command.status == TIMEOUT_UNCERTAIN:
                raise TimeoutError("Camera query acknowledgement timed out; result is uncertain")
            raise RuntimeError(command.error_detail or "Camera query failed")
        body = (command.response_metadata or {}).get("access_list_body")
        return select_record({"payload": {"body": body}}, plate)

    def _execute(self, camera: Camera, command_type: str, body: dict, plate: str | None = None, *, metadata: dict | None = None) -> CameraCommand:
        command = self._command_manager.enqueue_access_list(
            camera_id=camera.id,
            serial_number=camera.serial_number,
            command_type=command_type,
            body=body,
            request_metadata=metadata if metadata is not None else {"plate": plate},
        )
        deadline = time.monotonic() + settings.visionzenith_command_timeout_seconds + 2
        while time.monotonic() < deadline and not self._stop.is_set():
            time.sleep(0.1)
            with self._session_factory() as lookup:
                current = lookup.scalar(select(CameraCommand).where(CameraCommand.id == command.id))
                if current is not None and current.status not in {"QUEUED", "PUBLISHED"}:
                    lookup.expunge(current)
                    return current
        raise TimeoutError("Camera command result is uncertain")

    @staticmethod
    def _command_failure(sync: AccessListCameraSync, command: CameraCommand) -> None:
        sync.status = {
            TIMEOUT_UNCERTAIN: "TIMEOUT_UNCERTAIN",
            CAMERA_REJECTED: "ERROR",
            PUBLISH_FAILED: "ERROR",
        }.get(command.status, "ERROR")
        sync.last_error = command.error_detail or "Camera update failed"

    @staticmethod
    def _apply_verification(sync: AccessListCameraSync, record: dict | None, changes: dict) -> None:
        now = datetime.now(timezone.utc)
        sync.last_verified_at = now
        if record is None:
            sync.status = "NOT_FOUND"
            sync.last_error = "Plate was not found on camera readback"
            sync.verification_details = {"missing_record": True}
            return
        missing, mismatches = verify_changes(record, changes)
        sync.remote_index = str(record.get("index"))[:64] if record.get("index") is not None else None
        sync.verification_details = {"unverified_fields": missing, "mismatched_fields": mismatches}
        if mismatches:
            sync.status = "MISMATCH"
            sync.last_error = "Camera readback differs for: " + ", ".join(mismatches)
        elif missing:
            sync.status = "PARTIALLY_VERIFIED"
            sync.last_error = "Camera did not return: " + ", ".join(missing)
        else:
            sync.status = "VERIFIED"
            sync.last_error = None
