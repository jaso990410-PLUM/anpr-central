from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.integrations.visionzenith.command_manager import VisionZenithCommandManager
from app.models.camera import Camera
from app.models.offline_recovery_run import OfflineRecoveryRun
from app.services.camera_service import CREDENTIALS_REVOKED


logger = logging.getLogger(__name__)

ACTIVE_STATUSES = {"BACKLOG_DETECTED", "REQUESTED", "RECOVERING"}
TERMINAL_STATUSES = {
    "COMPLETED",
    "STALLED",
    "PARTIAL",
    "TIMEOUT_UNCERTAIN",
    "CAMERA_OFFLINE",
    "ERROR",
}


class OfflineRecoveryRejected(ValueError):
    """A safe rejection of invalid recovery input or camera state."""


def parse_offline_count(payload: bytes, *, retained: bool = False) -> int:
    if retained:
        raise OfflineRecoveryRejected("Retained offline-count messages are not accepted")
    try:
        document = json.loads(payload.decode("utf-8"))
        count = document["payload"]["body"]["offline_record"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise OfflineRecoveryRejected("Malformed offline-count message") from error
    if type(count) is not int or count < 0 or count > 10_000_000:
        raise OfflineRecoveryRejected("Invalid offline record count")
    return count


def parse_recovery_ack(payload: bytes) -> tuple[str, int]:
    try:
        document = json.loads(payload.decode("utf-8"))
        request_id = document["id"]
        code = document["code"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise OfflineRecoveryRejected("Malformed offline-recovery reply") from error
    if not isinstance(request_id, str) or not request_id or isinstance(code, bool):
        raise OfflineRecoveryRejected("Malformed offline-recovery reply")
    try:
        normalized_code = int(code)
    except (TypeError, ValueError) as error:
        raise OfflineRecoveryRejected("Malformed offline-recovery reply") from error
    return request_id, normalized_code


class OfflineRecoveryCoordinator:
    """Coordinates explicit, bounded RA5 offline-event recovery batches.

    Camera count reports are the progress authority. A command acknowledgement only
    confirms that the camera accepted the request; it never completes a run.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        command_manager: VisionZenithCommandManager,
        is_connected: Callable[[], bool],
    ) -> None:
        self._session_factory = session_factory
        self._command_manager = command_manager
        self._is_connected = is_connected

    def start(self) -> None:
        # Recovery commands are deliberately not replayed after a Central restart.
        with self._session_factory() as db:
            runs = db.scalars(
                select(OfflineRecoveryRun).where(
                    OfflineRecoveryRun.status.in_(ACTIVE_STATUSES)
                )
            ).all()
            if not runs:
                return
            now = datetime.now(timezone.utc)
            for run in runs:
                run.status = "TIMEOUT_UNCERTAIN"
                run.completed_at = now
                run.last_error = "ANPR Central restarted while recovery was active"
            db.commit()

    def handle_count(
        self,
        serial_number: str,
        payload: bytes,
        *,
        retained: bool = False,
    ) -> OfflineRecoveryRun | None:
        count = parse_offline_count(payload, retained=retained)
        with self._session_factory() as db:
            camera = self._eligible_camera(db, serial_number)
            run = self._latest_run(db, camera.id)
            now = datetime.now(timezone.utc)
            is_connection_probe = (
                run is not None
                and run.status in ACTIVE_STATUSES
                and run.initial_reported_count == 0
                and run.requested_max_count == 1
            )

            if is_connection_probe:
                run.initial_reported_count = count + min(run.messages_received, 1)
                run.last_reported_count = count
                if count == 0:
                    run.status = "COMPLETED"
                    run.completed_at = now
                    run.last_error = None
                    db.commit()
                    return run
                db.commit()
                return self._request_batch(db, camera, run)

            if count == 0:
                if run is not None:
                    run.last_reported_count = 0
                    run.status = "COMPLETED"
                    run.completed_at = now
                    run.last_error = None
                    db.commit()
                return run

            if run is not None and run.status == "STALLED":
                run.last_reported_count = count
                db.commit()
                return run

            if run is None or run.status not in ACTIVE_STATUSES:
                run = OfflineRecoveryRun(
                    camera_id=camera.id,
                    status="BACKLOG_DETECTED",
                    initial_reported_count=count,
                    last_reported_count=count,
                    requested_max_count=settings.offline_recovery_batch_size,
                    requested_min_id=0,
                    started_at=now,
                )
                db.add(run)
                db.commit()
                db.refresh(run)
                return self._request_batch(db, camera, run)

            previous_count = run.last_reported_count
            inserted_this_batch = run.events_inserted - run.batch_start_inserted
            run.last_reported_count = count
            if count > previous_count:
                run.status = "PARTIAL"
                run.completed_at = now
                run.last_error = "Camera backlog increased during recovery; manual review required"
                db.commit()
                return run
            if count == previous_count and inserted_this_batch == 0:
                run.status = "STALLED"
                run.completed_at = now
                run.last_error = "Camera backlog did not decrease after a recovery batch"
                db.commit()
                return run

            db.commit()
            return self._request_batch(db, camera, run)

    def probe_connection(self, camera_id: UUID) -> OfflineRecoveryRun:
        """Request at most one record after a newly observed RA5 connection."""
        with self._session_factory() as db:
            camera = db.get(Camera, camera_id)
            if camera is None:
                raise OfflineRecoveryRejected("Camera not found")
            self._validate_camera(camera)
            if camera.connection_state != "ONLINE":
                raise OfflineRecoveryRejected("Camera is offline")
            latest = self._latest_run(db, camera.id)
            if latest is not None and latest.status in ACTIVE_STATUSES:
                return latest
            if latest is not None and latest.status == "STALLED":
                return latest
            run = OfflineRecoveryRun(
                camera_id=camera.id,
                status="BACKLOG_DETECTED",
                initial_reported_count=0,
                last_reported_count=0,
                requested_max_count=1,
                requested_min_id=0,
                started_at=datetime.now(timezone.utc),
            )
            db.add(run)
            db.commit()
            db.refresh(run)
            return self._request_batch(db, camera, run, max_count=1)

    def handle_ack(self, serial_number: str, payload: bytes) -> OfflineRecoveryRun | None:
        request_id, code = parse_recovery_ack(payload)
        with self._session_factory() as db:
            camera = self._eligible_camera(db, serial_number)
            run = db.scalar(
                select(OfflineRecoveryRun)
                .where(
                    OfflineRecoveryRun.camera_id == camera.id,
                    OfflineRecoveryRun.command_request_id == request_id,
                )
                .order_by(OfflineRecoveryRun.started_at.desc())
            )
            if run is None:
                return None
            if code == 200:
                run.status = "RECOVERING"
                run.last_error = None
            else:
                run.status = "ERROR"
                run.completed_at = datetime.now(timezone.utc)
                run.last_error = f"Camera rejected recovery command with code {code}"
            db.commit()
            return run

    def record_message(self, serial_number: str, created: bool, valid: bool) -> None:
        try:
            with self._session_factory() as db:
                camera = db.scalar(
                    select(Camera).where(Camera.serial_number == serial_number)
                )
                if camera is None:
                    return
                run = self._latest_active_run(db, camera.id)
                if run is None:
                    return
                run.messages_received += 1
                run.last_message_at = datetime.now(timezone.utc)
                run.status = "RECOVERING"
                if not valid:
                    run.invalid_skipped += 1
                elif created:
                    run.events_inserted += 1
                else:
                    run.duplicates_skipped += 1
                db.commit()
        except Exception:
            logger.exception("Unable to update offline recovery progress")

    def mark_timeouts(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=settings.offline_recovery_timeout_seconds
        )
        with self._session_factory() as db:
            runs = db.scalars(
                select(OfflineRecoveryRun).where(
                    OfflineRecoveryRun.status.in_(ACTIVE_STATUSES)
                )
            ).all()
            changed = 0
            now = datetime.now(timezone.utc)
            for run in runs:
                activity = run.last_message_at or run.started_at
                if _utc(activity) >= cutoff:
                    continue
                run.status = "TIMEOUT_UNCERTAIN"
                run.completed_at = now
                run.last_error = "No offline recovery progress was observed before timeout"
                changed += 1
            if changed:
                db.commit()
            return changed

    def mark_camera_offline_runs(self) -> int:
        with self._session_factory() as db:
            runs = db.scalars(
                select(OfflineRecoveryRun)
                .join(Camera, Camera.id == OfflineRecoveryRun.camera_id)
                .where(
                    OfflineRecoveryRun.status.in_(ACTIVE_STATUSES),
                    Camera.connection_state == "OFFLINE",
                )
            ).all()
            now = datetime.now(timezone.utc)
            for run in runs:
                run.status = "CAMERA_OFFLINE"
                run.completed_at = now
                run.last_error = "Camera went offline during recovery"
            if runs:
                db.commit()
            return len(runs)

    def manual_recover(self, camera_id: UUID) -> OfflineRecoveryRun:
        with self._session_factory() as db:
            camera = db.get(Camera, camera_id)
            if camera is None:
                raise OfflineRecoveryRejected("Camera not found")
            self._validate_camera(camera)
            if camera.connection_state != "ONLINE":
                raise OfflineRecoveryRejected("Camera is offline")
            latest = self._latest_run(db, camera.id)
            if latest is not None and latest.status in ACTIVE_STATUSES:
                raise OfflineRecoveryRejected("Offline recovery is already active")
            count = latest.last_reported_count if latest is not None else 0
            if count <= 0:
                raise OfflineRecoveryRejected("No offline camera backlog has been reported")
            run = OfflineRecoveryRun(
                camera_id=camera.id,
                status="BACKLOG_DETECTED",
                initial_reported_count=count,
                last_reported_count=count,
                requested_max_count=settings.offline_recovery_batch_size,
                requested_min_id=0,
                started_at=datetime.now(timezone.utc),
            )
            db.add(run)
            db.commit()
            db.refresh(run)
            return self._request_batch(db, camera, run)

    def _request_batch(
        self,
        db: Session,
        camera: Camera,
        run: OfflineRecoveryRun,
        *,
        max_count: int | None = None,
    ) -> OfflineRecoveryRun:
        if not self._is_connected():
            run.status = "CAMERA_OFFLINE"
            run.completed_at = datetime.now(timezone.utc)
            run.last_error = "ANPR Central is not connected to the MQTT broker"
            db.commit()
            return run
        requested_count = max_count or min(
            settings.offline_recovery_batch_size,
            max(1, run.last_reported_count),
        )
        command = self._command_manager.enqueue_offline_recovery(
            camera_id=camera.id,
            serial_number=camera.serial_number or "",
            max_count=requested_count,
            min_id=0,
        )
        run.status = "REQUESTED"
        run.command_request_id = command.request_id
        run.requested_max_count = requested_count
        run.requested_min_id = 0
        run.batch_start_messages = run.messages_received
        run.batch_start_inserted = run.events_inserted
        run.completed_at = None
        run.last_error = None
        db.commit()
        db.refresh(run)
        return run

    @staticmethod
    def _latest_run(db: Session, camera_id: UUID) -> OfflineRecoveryRun | None:
        return db.scalar(
            select(OfflineRecoveryRun)
            .where(OfflineRecoveryRun.camera_id == camera_id)
            .order_by(OfflineRecoveryRun.started_at.desc(), OfflineRecoveryRun.id.desc())
            .limit(1)
        )

    @staticmethod
    def _latest_active_run(db: Session, camera_id: UUID) -> OfflineRecoveryRun | None:
        return db.scalar(
            select(OfflineRecoveryRun)
            .where(
                OfflineRecoveryRun.camera_id == camera_id,
                OfflineRecoveryRun.status.in_(ACTIVE_STATUSES),
            )
            .order_by(OfflineRecoveryRun.started_at.desc(), OfflineRecoveryRun.id.desc())
            .limit(1)
        )

    def _eligible_camera(self, db: Session, serial_number: str) -> Camera:
        camera = db.scalar(select(Camera).where(Camera.serial_number == serial_number))
        if camera is None:
            raise OfflineRecoveryRejected("Unknown camera serial number")
        self._validate_camera(camera)
        return camera

    @staticmethod
    def _validate_camera(camera: Camera) -> None:
        if not camera.is_enabled:
            raise OfflineRecoveryRejected("Camera is disabled")
        if camera.provisioning_state == CREDENTIALS_REVOKED:
            raise OfflineRecoveryRejected("Camera provisioning is revoked")
        if not camera.serial_number:
            raise OfflineRecoveryRejected("Camera serial number is required")


def recovery_summary(db: Session, camera_id: UUID) -> dict[str, Any]:
    run = OfflineRecoveryCoordinator._latest_run(db, camera_id)
    if run is None:
        return {
            "status": "IDLE",
            "backlog": 0,
            "messages_received": 0,
            "events_inserted": 0,
            "duplicates_skipped": 0,
            "invalid_skipped": 0,
            "last_error": None,
            "active": False,
            "can_retry": False,
        }
    return {
        "status": run.status,
        "backlog": run.last_reported_count,
        "messages_received": run.messages_received,
        "events_inserted": run.events_inserted,
        "duplicates_skipped": run.duplicates_skipped,
        "invalid_skipped": run.invalid_skipped,
        "last_error": run.last_error,
        "active": run.status in ACTIVE_STATUSES,
        "can_retry": run.status in TERMINAL_STATUSES and run.last_reported_count > 0,
    }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
