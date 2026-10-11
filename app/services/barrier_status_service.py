from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.visionzenith.command_manager import VisionZenithCommandManager
from app.models.barrier import BarrierConfiguration, BarrierState
from app.models.camera import Camera
from app.models.camera_command import CameraCommand
from app.services.camera_service import CREDENTIALS_REVOKED, ONLINE


VALID_STATES = {"UNKNOWN", "CLOSED", "OPEN", "INTERMEDIATE", "INVALID_FEEDBACK"}


class BarrierFeedbackRejected(ValueError):
    pass


def normalize_gpio_feedback(
    *,
    open_raw: int | None,
    open_level: str,
    closed_raw: int | None,
    closed_level: str,
    has_open: bool,
    has_closed: bool,
) -> str:
    def active(value: int | None, level: str) -> bool | None:
        if value not in (0, 1):
            return None
        return value == (1 if level == "HIGH" else 0)

    opened = active(open_raw, open_level) if has_open else None
    closed = active(closed_raw, closed_level) if has_closed else None
    if has_open and has_closed:
        if opened is None or closed is None:
            return "UNKNOWN"
        if opened and closed:
            return "INVALID_FEEDBACK"
        if opened:
            return "OPEN"
        if closed:
            return "CLOSED"
        return "INTERMEDIATE"
    if has_open:
        return "OPEN" if opened else "UNKNOWN"
    if has_closed:
        return "CLOSED" if closed else "UNKNOWN"
    return "UNKNOWN"


class BarrierStatusService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_or_create(self, camera_id) -> BarrierState:
        state = self.db.scalar(select(BarrierState).where(BarrierState.camera_id == camera_id))
        if state is None:
            state = BarrierState(camera_id=camera_id, normalized_state="UNKNOWN", feedback_source="NONE")
            self.db.add(state)
            self.db.flush()
        return state

    def handle_native_status(self, serial_number: str, payload: bytes, *, now: datetime | None = None) -> BarrierState:
        document = _document(payload)
        if str(document.get("sn", serial_number)) != serial_number:
            raise BarrierFeedbackRejected("Topic and payload camera serial do not match")
        camera = self.db.scalar(select(Camera).where(Camera.serial_number == serial_number))
        if camera is None or not camera.is_enabled or camera.provisioning_state == CREDENTIALS_REVOKED:
            raise BarrierFeedbackRejected("Camera is not eligible for barrier feedback")
        config = self.db.scalar(select(BarrierConfiguration).where(BarrierConfiguration.camera_id == camera.id))
        if config is None or config.feedback_mode != "NATIVE_GATE_STATUS":
            raise BarrierFeedbackRejected("Native barrier feedback is not configured")
        body = (document.get("payload") or {}).get("body")
        if not isinstance(body, dict):
            raise BarrierFeedbackRejected("Malformed native barrier status")
        gate_status, connect_status, enabled = body.get("gate_status"), body.get("connect_status"), body.get("enable")
        if gate_status not in (0, 1, 2) or connect_status not in (0, 1) or enabled not in (0, 1):
            raise BarrierFeedbackRejected("Malformed native barrier status values")
        normalized = {0: "CLOSED", 1: "OPEN", 2: "INTERMEDIATE"}[gate_status] if connect_status == 1 and enabled == 1 else "UNKNOWN"
        state = self.get_or_create(camera.id)
        state.connect_status = connect_status
        state.gate_enabled = enabled == 1
        gate_ctrl_id = body.get("gate_ctrl_id")
        state.gate_ctrl_id = str(gate_ctrl_id)[:100] if gate_ctrl_id is not None else None
        self._observe(state, normalized, "NATIVE_GATE_STATUS", now or datetime.now(timezone.utc))
        return state

    def handle_gpio_event(self, serial_number: str, payload: bytes, command_manager: VisionZenithCommandManager) -> int:
        document = _document(payload)
        if str(document.get("sn", serial_number)) != serial_number:
            raise BarrierFeedbackRejected("Topic and payload camera serial do not match")
        try:
            trigger = document["payload"]["body"]["AlarmGioIn"]["result"]["TriggerResult"]
            source, value = trigger["source"], trigger["value"]
        except (KeyError, TypeError) as error:
            raise BarrierFeedbackRejected("Malformed GPIO input event") from error
        if type(source) is not int or value not in (0, 1, 2):
            raise BarrierFeedbackRejected("Malformed GPIO input event values")
        camera = self.db.scalar(select(Camera).where(Camera.serial_number == serial_number))
        if camera is None:
            raise BarrierFeedbackRejected("Unknown camera serial number")
        config = self.db.scalar(select(BarrierConfiguration).where(BarrierConfiguration.camera_id == camera.id))
        if config is None or config.feedback_mode != "GPIO_INPUTS":
            return 0
        if source not in {config.open_status_input, config.closed_status_input}:
            return 0
        return self.reconcile_gpio(camera, config, command_manager)

    def reconcile_gpio(self, camera: Camera, config: BarrierConfiguration, command_manager: VisionZenithCommandManager) -> int:
        if config.feedback_mode != "GPIO_INPUTS" or not camera.serial_number:
            return 0
        inputs = {value for value in (config.open_status_input, config.closed_status_input) if value is not None}
        for gpio in sorted(inputs):
            command_manager.enqueue_io_status(camera_id=camera.id, serial_number=camera.serial_number, gpio=gpio)
        return len(inputs)

    def mark_camera_unknown(self, camera_id) -> None:
        state = self.db.scalar(select(BarrierState).where(BarrierState.camera_id == camera_id))
        if state is not None:
            state.normalized_state = "UNKNOWN"
            state.observed_at = None

    def mark_offline_states_unknown(self) -> int:
        states = self.db.scalars(select(BarrierState).join(Camera).where(Camera.connection_state != ONLINE, BarrierState.normalized_state != "UNKNOWN")).all()
        for state in states:
            state.normalized_state = "UNKNOWN"
            state.observed_at = None
        return len(states)

    @staticmethod
    def apply_gpio_readback(db: Session, command: CameraCommand, document: dict[str, Any], observed_at: datetime) -> BarrierState:
        config = db.scalar(select(BarrierConfiguration).where(BarrierConfiguration.camera_id == command.camera_id))
        if config is None or config.feedback_mode != "GPIO_INPUTS":
            raise BarrierFeedbackRejected("GPIO barrier feedback is not configured")
        payload = document.get("payload")
        body = payload.get("body") if isinstance(payload, dict) else None
        if not isinstance(body, dict) or type(body.get("gpio")) is not int or body.get("status") not in (0, 1):
            raise BarrierFeedbackRejected("Malformed GPIO status readback")
        gpio, raw = body["gpio"], body["status"]
        requested = (command.request_metadata or {}).get("gpio")
        if gpio != requested or gpio not in {config.open_status_input, config.closed_status_input}:
            raise BarrierFeedbackRejected("GPIO readback does not match the request")
        state = db.scalar(select(BarrierState).where(BarrierState.camera_id == command.camera_id))
        if state is None:
            state = BarrierState(camera_id=command.camera_id)
            db.add(state)
        if gpio == config.open_status_input:
            state.open_input_raw = raw
        if gpio == config.closed_status_input:
            state.closed_input_raw = raw
        normalized = normalize_gpio_feedback(
            open_raw=state.open_input_raw, open_level=config.open_active_level,
            closed_raw=state.closed_input_raw, closed_level=config.closed_active_level,
            has_open=config.open_status_input is not None, has_closed=config.closed_status_input is not None,
        )
        BarrierStatusService._observe(state, normalized, "GPIO_INPUTS", observed_at)
        return state

    @staticmethod
    def _observe(state: BarrierState, normalized: str, source: str, observed_at: datetime) -> None:
        state.normalized_state = normalized if normalized in VALID_STATES else "UNKNOWN"
        state.feedback_source = source
        state.observed_at = observed_at if state.normalized_state != "UNKNOWN" else None
        if state.normalized_state in {"CLOSED", "OPEN", "INTERMEDIATE"}:
            state.last_known_state = state.normalized_state
            state.last_known_at = observed_at


def _document(payload: bytes) -> dict[str, Any]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BarrierFeedbackRejected("Malformed barrier feedback JSON") from error
    if not isinstance(document, dict):
        raise BarrierFeedbackRejected("Malformed barrier feedback document")
    return document
