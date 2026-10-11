from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from threading import Event, Thread
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core.config import settings
from app.integrations.visionzenith.barrier import VisionZenithBarrierAdapter
from app.integrations.visionzenith.command_manager import VisionZenithCommandManager
from app.models.barrier import BarrierAction, BarrierConfiguration, BarrierState
from app.models.camera import Camera
from app.models.parking import AccessDecision, ParkingReviewItem
from app.models.recognition_event import RecognitionEvent
from app.services.barrier_status_service import BarrierStatusService
from app.services.camera_service import CREDENTIALS_REVOKED, ONLINE


logger = logging.getLogger(__name__)


class BarrierActionError(ValueError):
    pass


class BarrierActionCoordinator:
    """Create and execute durable OPEN actions without replay or blind retry."""

    def __init__(self, *, session_factory, command_manager: VisionZenithCommandManager, is_connected) -> None:
        self._session_factory = session_factory
        self._commands = command_manager
        self._adapter = VisionZenithBarrierAdapter(command_manager)
        self._is_connected = is_connected
        self._stop = Event()
        self._thread: Thread | None = None
        self._last_gpio_check: dict[UUID, datetime] = {}
        self._restart_reconciled = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._confirmation_loop, name="anpr-barrier-actions", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None

    def create_automatic(self, decision_id: UUID) -> UUID | None:
        self._ensure_restart_reconciled()
        now = datetime.now(timezone.utc)
        with self._session_factory() as db:
            existing = db.scalar(select(BarrierAction).where(BarrierAction.access_decision_id == decision_id, BarrierAction.trigger_source == "AUTO_DECISION"))
            if existing is not None:
                return existing.id
            decision = db.scalar(
                select(AccessDecision)
                .options(selectinload(AccessDecision.recognition_event))
                .where(AccessDecision.id == decision_id)
            )
            if decision is None or not self._automatic_decision_eligible(decision):
                return None
            camera = db.get(Camera, decision.camera_id)
            config = db.scalar(select(BarrierConfiguration).where(BarrierConfiguration.camera_id == decision.camera_id))
            if camera is None or not self._automatic_configuration_eligible(camera, config) or not self._camera_eligible(camera):
                return None
            expires_at = _utc(decision.evaluated_at) + timedelta(seconds=settings.barrier_action_max_age_seconds)
            action = BarrierAction(
                camera_id=camera.id, site_id=decision.site_id,
                recognition_event_id=decision.recognition_event_id,
                access_decision_id=decision.id, action_type="OPEN",
                trigger_source="AUTO_DECISION", command_status="QUEUED",
                requested_at=now, expires_at=expires_at,
                physical_confirmation_status="NOT_AVAILABLE",
            )
            db.add(action)
            decision.physical_execution = "ACTION_CREATED"
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.scalar(select(BarrierAction).where(BarrierAction.access_decision_id == decision_id, BarrierAction.trigger_source == "AUTO_DECISION"))
                return existing.id if existing else None
            action_id = action.id
        self.execute(action_id)
        return action_id

    def create_manual(self, *, camera_id: UUID, user_id: UUID, reason: str, review_item_id: UUID | None = None) -> UUID:
        self._ensure_restart_reconciled()
        clean_reason = reason.strip()
        if len(clean_reason) < 3:
            raise BarrierActionError("Enter a meaningful reason for the manual barrier release.")
        with self._session_factory() as db:
            camera = db.get(Camera, camera_id)
            config = db.scalar(select(BarrierConfiguration).where(BarrierConfiguration.camera_id == camera_id))
            if camera is None or not self._manual_configuration_eligible(camera, config) or not self._camera_eligible(camera) or not self._is_connected():
                raise BarrierActionError("This barrier is not commissioned for Central manual operation.")
            review = db.get(ParkingReviewItem, review_item_id) if review_item_id else None
            if review_item_id and (review is None or review.access_decision.camera_id != camera_id):
                raise BarrierActionError("Review item does not belong to the selected barrier.")
            action = self._new_operator_action(camera, user_id, clean_reason, "MANUAL_RELEASE", review)
            db.add(action); db.commit(); action_id = action.id
        self.execute(action_id)
        return action_id

    def create_test(self, *, camera_id: UUID, user_id: UUID, reason: str = "Commissioning gate-open test") -> UUID:
        self._ensure_restart_reconciled()
        with self._session_factory() as db:
            camera = db.get(Camera, camera_id)
            config = db.scalar(select(BarrierConfiguration).where(BarrierConfiguration.camera_id == camera_id))
            if camera is None or config is None or not config.enabled or config.control_mode != "CENTRAL_MANAGED" or not self._camera_eligible(camera) or not self._is_connected():
                raise BarrierActionError("Enable Central Managed barrier control before testing.")
            action = self._new_operator_action(camera, user_id, reason, "COMMISSIONING_TEST", None)
            db.add(action); db.commit(); action_id = action.id
        self.execute(action_id)
        return action_id

    def execute(self, action_id: UUID) -> None:
        with self._session_factory() as db:
            action = db.get(BarrierAction, action_id)
            if action is None or action.command_status != "QUEUED":
                return
            now = datetime.now(timezone.utc)
            if _utc(action.expires_at) <= now:
                action.command_status = "EXPIRED_NOT_SENT"
                action.last_error = "Barrier action expired before publication"
                db.commit(); return
            camera = db.get(Camera, action.camera_id)
            config = db.scalar(select(BarrierConfiguration).where(BarrierConfiguration.camera_id == action.camera_id))
            eligible = self._automatic_configuration_eligible(camera, config) if action.trigger_source == "AUTO_DECISION" else (
                config is not None and config.enabled and config.control_mode == "CENTRAL_MANAGED" and
                (action.trigger_source == "COMMISSIONING_TEST" or config.commissioned_at is not None)
            )
            if camera is None or not eligible:
                action.command_status = "NOT_CONFIGURED"
                action.last_error = "Barrier configuration is not eligible for this action"
                db.commit(); return
            if not self._camera_eligible(camera) or not self._is_connected():
                action.command_status = "PUBLISH_FAILED"
                action.last_error = "Camera or MQTT connection is unavailable; action will not be replayed"
                db.commit(); return
            state = db.scalar(select(BarrierState).where(BarrierState.camera_id == camera.id))
            if action.trigger_source == "AUTO_DECISION" and state is not None and state.normalized_state == "OPEN" and state.observed_at is not None and state.feedback_source == config.feedback_mode:
                action.command_status = "SUPPRESSED_ALREADY_OPEN"
                action.physical_confirmation_status = "CONFIRMED_OPEN"
                action.physical_confirmed_at = state.observed_at
                db.commit(); return
            db.expunge(action); db.expunge(camera)
        try:
            self._adapter.open(action, camera)
        except Exception as error:
            with self._session_factory() as db:
                current = db.get(BarrierAction, action_id)
                if current is not None and current.command_status == "QUEUED":
                    current.command_status = "PUBLISH_FAILED"
                    current.last_error = str(error)[:500]
                    db.commit()

    def reconcile_feedback(self, camera_id: UUID) -> int:
        if not self._is_connected():
            raise BarrierActionError("MQTT broker is not connected.")
        with self._session_factory() as db:
            camera = db.get(Camera, camera_id)
            config = db.scalar(select(BarrierConfiguration).where(BarrierConfiguration.camera_id == camera_id))
            if camera is None or config is None:
                raise BarrierActionError("Barrier configuration was not found.")
            if config.feedback_mode == "NONE":
                BarrierStatusService(db).mark_camera_unknown(camera.id)
                db.commit(); return 0
            if config.feedback_mode == "NATIVE_GATE_STATUS":
                BarrierStatusService(db).mark_camera_unknown(camera.id)
                db.commit(); return 0
            return BarrierStatusService(db).reconcile_gpio(camera, config, self._commands)

    def camera_connected(self, camera_id: UUID) -> None:
        try:
            with self._session_factory() as db:
                camera = db.get(Camera, camera_id)
                config = db.scalar(select(BarrierConfiguration).where(BarrierConfiguration.camera_id == camera_id))
                if camera and config and config.feedback_mode == "GPIO_INPUTS":
                    BarrierStatusService(db).reconcile_gpio(camera, config, self._commands)
                elif camera and config and config.feedback_mode == "NATIVE_GATE_STATUS":
                    BarrierStatusService(db).mark_camera_unknown(camera.id); db.commit()
        except Exception:
            logger.exception("Barrier feedback reconnect reconciliation failed", extra={"camera_id": str(camera_id)})

    def _reconcile_restart(self) -> None:
        gpio_camera_ids: list[UUID] = []
        with self._session_factory() as db:
            for action in db.scalars(select(BarrierAction).where(BarrierAction.command_status == "QUEUED")).all():
                action.command_status = "EXPIRED_NOT_SENT"
                action.last_error = "Pending physical action was not replayed after application restart"
            for action in db.scalars(select(BarrierAction).where(BarrierAction.command_status == "PUBLISHED")).all():
                action.command_status = "TIMEOUT_UNCERTAIN"
                action.last_error = "Published physical action had no known reply at application restart"
            for state in db.scalars(select(BarrierState)).all():
                state.normalized_state = "UNKNOWN"
                state.observed_at = None
            gpio_camera_ids = list(db.scalars(
                select(BarrierConfiguration.camera_id)
                .join(Camera, Camera.id == BarrierConfiguration.camera_id)
                .where(BarrierConfiguration.feedback_mode == "GPIO_INPUTS", Camera.connection_state == ONLINE)
            ).all())
            db.commit()
        for camera_id in gpio_camera_ids:
            try:
                self.reconcile_feedback(camera_id)
            except Exception:
                logger.warning("Startup GPIO barrier reconciliation could not be queued", extra={"camera_id": str(camera_id)})

    def _confirmation_loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self._ensure_restart_reconciled()
                self._update_confirmations()
            except Exception:
                logger.exception("Barrier physical-confirmation check failed")

    def _ensure_restart_reconciled(self) -> None:
        if self._restart_reconciled:
            return
        self._reconcile_restart()
        self._restart_reconciled = True

    def _update_confirmations(self) -> None:
        now = datetime.now(timezone.utc)
        gpio_reconcile: list[UUID] = []
        with self._session_factory() as db:
            actions = db.scalars(select(BarrierAction).where(
                BarrierAction.command_status == "CAMERA_ACKNOWLEDGED",
                BarrierAction.physical_confirmation_status == "PENDING",
            )).all()
            for action in actions:
                config = db.scalar(select(BarrierConfiguration).where(BarrierConfiguration.camera_id == action.camera_id))
                state = db.scalar(select(BarrierState).where(BarrierState.camera_id == action.camera_id))
                acknowledged = _utc(action.camera_ack_at or action.requested_at)
                if state and state.normalized_state == "OPEN" and state.observed_at and _utc(state.observed_at) >= acknowledged:
                    action.physical_confirmation_status = "CONFIRMED_OPEN"
                    action.physical_confirmed_at = state.observed_at
                    continue
                timeout = config.open_confirmation_timeout_seconds if config else 10
                if now >= acknowledged + timedelta(seconds=timeout):
                    action.physical_confirmation_status = "FEEDBACK_NOT_OBSERVED"
                    continue
                if config and config.feedback_mode == "GPIO_INPUTS" and self._is_connected():
                    last = self._last_gpio_check.get(action.camera_id)
                    if last is None or now - last >= timedelta(seconds=3):
                        gpio_reconcile.append(action.camera_id)
                        self._last_gpio_check[action.camera_id] = now
            db.commit()
        for camera_id in gpio_reconcile:
            try:
                self.reconcile_feedback(camera_id)
            except Exception:
                logger.warning("GPIO post-open reconciliation could not be queued", extra={"camera_id": str(camera_id)})

    @staticmethod
    def _automatic_decision_eligible(decision: AccessDecision) -> bool:
        event: RecognitionEvent = decision.recognition_event
        return event is not None and event.source == "LIVE" and decision.decision == "ALLOW" and decision.barrier_intent == "OPEN"

    @classmethod
    def _automatic_configuration_eligible(cls, camera: Camera | None, config: BarrierConfiguration | None) -> bool:
        return bool(camera and config and config.enabled and config.control_mode == "CENTRAL_MANAGED" and config.commissioned_at is not None and config.local_auto_open_verified and camera.lane_role in {"ENTRY", "EXIT"})

    @staticmethod
    def _manual_configuration_eligible(camera: Camera, config: BarrierConfiguration | None) -> bool:
        return bool(config and config.enabled and config.control_mode == "CENTRAL_MANAGED" and config.commissioned_at is not None and config.local_auto_open_verified and camera.lane_role in {"ENTRY", "EXIT"})

    @staticmethod
    def _camera_eligible(camera: Camera) -> bool:
        return bool(camera.is_enabled and camera.connection_state == ONLINE and camera.serial_number and camera.provisioning_state != CREDENTIALS_REVOKED)

    @staticmethod
    def _new_operator_action(camera: Camera, user_id: UUID, reason: str, trigger: str, review: ParkingReviewItem | None) -> BarrierAction:
        now = datetime.now(timezone.utc)
        decision = review.access_decision if review else None
        return BarrierAction(
            camera_id=camera.id, site_id=camera.site_id,
            recognition_event_id=review.recognition_event_id if review else None,
            access_decision_id=decision.id if decision else None,
            parking_review_item_id=review.id if review else None,
            trigger_source=trigger, command_status="QUEUED", action_type="OPEN",
            requested_at=now, expires_at=now + timedelta(seconds=settings.barrier_action_max_age_seconds),
            requested_by_user_id=user_id, operator_reason=reason[:500],
            physical_confirmation_status="NOT_AVAILABLE",
        )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
