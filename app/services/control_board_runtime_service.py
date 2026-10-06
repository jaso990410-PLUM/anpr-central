from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event, Lock, Thread, Timer
from collections.abc import Callable
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.camera import Camera
from app.models.control_board import ControlBoard, PeripheralBehavior, PeripheralPreset, PeripheralPresetSchedule
from app.integrations.visionzenith.command_manager import VisionZenithCommandManager
from app.integrations.visionzenith.protocol import display_settings_frame, idle_frame, led_frame, prepare_text, traffic_light_frame, voice_frame
from app.integrations.visionzenith.rs485 import get_rs485_body, set_rs485_body
from app.services.camera_service import ONLINE
from app.core.config import settings


@dataclass(frozen=True)
class PeripheralContext:
    plate: str = ""
    access_status: str = ""


@dataclass(frozen=True)
class ResolvedPeripheralState:
    preset_id: UUID | None
    preset_name: str
    mode: str
    rows: tuple[str, str, str, str]
    traffic_state: str
    green_seconds: int
    led_brightness: int
    led_scroll_speed: int
    speaker_enabled: bool
    speaker_message: str
    speaker_volume: int
    speaker_speed: int
    speaker_voice: int
    speaker_pitch: int
    plate_pause_ms: int


class ControlBoardRuntimeService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def resolve(
        self,
        board: ControlBoard,
        *,
        at: datetime | None = None,
        context: PeripheralContext | None = None,
    ) -> ResolvedPeripheralState:
        now = _utc(at or datetime.now(timezone.utc))
        context = context or PeripheralContext()
        preset = self._active_preset(board, now)
        if preset is None:
            return ResolvedPeripheralState(None, "Safe fallback", "IDLE", ("", "", "", ""), "RED", 5, 5, 5, False, "", 7, 5, 3, 5, 100)
        local = now.astimezone(_zone(board.camera.site.timezone))
        mode = "BUSY" if any(period.day_of_week == local.weekday() and period.start_time <= local.time().replace(tzinfo=None) < period.end_time for period in preset.busy_periods) else "IDLE"
        behavior = next(item for item in preset.behaviors if item.mode == mode)
        rows_by_number = {row.row_number: row for row in behavior.led_rows}
        rows = tuple(self._row(rows_by_number[number], board, local, context) for number in range(1, 5))
        message = behavior.speaker_message
        if "{plate}" in message:
            message = (
                prepare_text(message, context.plate, behavior.plate_pause_ms)
                if context.plate
                else message.replace("{plate}", "")
            )
        return ResolvedPeripheralState(
            preset.id, preset.name, mode, rows, behavior.traffic_state,
            behavior.traffic_green_seconds, behavior.led_brightness,
            behavior.led_scroll_speed, behavior.speaker_enabled, message,
            behavior.speaker_volume, behavior.speaker_speed, behavior.speaker_voice,
            behavior.speaker_pitch, behavior.plate_pause_ms,
        )

    def _active_preset(self, board: ControlBoard, now: datetime) -> PeripheralPreset | None:
        schedules = [item for item in board.schedules if item.enabled and _utc(item.starts_at) <= now < _utc(item.ends_at) and item.preset.enabled]
        if len(schedules) > 1:
            return None
        if schedules:
            return self._load_preset(schedules[0].preset_id)
        if board.default_preset_id and board.default_preset and board.default_preset.enabled:
            return self._load_preset(board.default_preset_id)
        return None

    def _load_preset(self, preset_id: UUID) -> PeripheralPreset:
        return self.db.scalar(
            select(PeripheralPreset)
            .options(
                selectinload(PeripheralPreset.behaviors).selectinload(PeripheralBehavior.led_rows),
                selectinload(PeripheralPreset.busy_periods),
            )
            .where(PeripheralPreset.id == preset_id)
        )

    @staticmethod
    def _row(row, board: ControlBoard, local: datetime, context: PeripheralContext) -> str:
        if row.content_type == "CUSTOM":
            return row.custom_text or ""
        values = {
            "BLANK": "",
            "RECOGNIZED_PLATE": context.plate,
            "ACCESS_STATUS": context.access_status,
            "CAMERA_NAME": board.camera.name,
            "SITE_NAME": board.camera.site.name,
            "CAMERA_ROLE": board.camera.direction.title(),
            "CURRENT_DATE": local.strftime("%d/%m/%Y"),
            "CURRENT_TIME": local.strftime("%H:%M:%S"),
        }
        return values.get(row.standard_source, "") or ""


def board_query():
    return select(ControlBoard).options(
        selectinload(ControlBoard.camera).selectinload(Camera.site),
        selectinload(ControlBoard.default_preset),
        selectinload(ControlBoard.schedules).selectinload(PeripheralPresetSchedule.preset),
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone.utc


class PeripheralRuntimeCoordinator:
    """Applies central preset transitions; never retries uncertain commands."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        command_manager: VisionZenithCommandManager,
        is_connected: Callable[[], bool],
    ) -> None:
        self._session_factory = session_factory
        self._commands = command_manager
        self._is_connected = is_connected
        self._stop = Event()
        self._thread: Thread | None = None
        self._signatures: dict[UUID, tuple[object, ...]] = {}
        self._timers: set[Timer] = set()
        self._lock = Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._loop, name="anpr-control-board-runtime", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            timers = tuple(self._timers)
        for timer in timers:
            timer.cancel()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None

    def restore_camera(self, camera_id: UUID, *, force: bool = False) -> None:
        with self._session_factory() as db:
            board = db.scalar(board_query().where(ControlBoard.camera_id == camera_id))
            if board:
                self._apply(db, board, force=force)

    def schedule_restore(self, camera_id: UUID, delay_seconds: float = 5.0) -> None:
        def restore() -> None:
            try:
                self.restore_camera(camera_id, force=True)
            finally:
                with self._lock:
                    self._timers.discard(timer)
        timer = Timer(delay_seconds, restore)
        timer.daemon = True
        with self._lock:
            self._timers.add(timer)
        timer.start()

    def handle_recognition(self, camera_id: UUID, plate: str) -> None:
        """Apply event speaker behavior and restore steady state afterward."""
        with self._session_factory() as db:
            board = db.scalar(board_query().where(ControlBoard.camera_id == camera_id))
            if board is None:
                return
            try:
                self._usable(board)
                state = ControlBoardRuntimeService(db).resolve(
                    board, context=PeripheralContext(plate=plate)
                )
                if state.speaker_enabled and state.speaker_message:
                    self._enqueue(
                        board,
                        "preset_speaker",
                        voice_frame(
                            state.speaker_message,
                            speed=state.speaker_speed,
                            volume=state.speaker_volume,
                            voice=state.speaker_voice,
                            pitch=state.speaker_pitch,
                        ),
                        qos=1,
                        metadata={
                            "preset": state.preset_name,
                            "mode": state.mode,
                            "recognition_triggered": True,
                        },
                    )
            except (RuntimeError, ValueError, UnicodeError):
                return
        self.schedule_restore(camera_id)

    def test(self, board_id: UUID, action: str) -> int:
        with self._session_factory() as db:
            board = db.scalar(board_query().where(ControlBoard.id == board_id))
            if board is None:
                raise RuntimeError("Control board not found")
            self._usable(board)
            if action in {"board", "led"}:
                frames = [("control_board_led_test", led_frame("ANPR", address=board.slave_id), 0)]
            elif action == "red":
                frames = [("control_board_test_red", traffic_light_frame(red=True, address=board.slave_id), 0)]
            elif action == "green":
                frames = [("control_board_test_green", traffic_light_frame(seconds=5, address=board.slave_id), 0)]
            elif action == "speaker":
                frames = [("control_board_speaker_test", voice_frame("ANPR Central test", speed=5, volume=7, voice=3, pitch=5), 0)]
            else:
                raise RuntimeError("Unknown control-board test")
            for command_type, frame, qos in frames:
                self._enqueue(board, command_type, frame, qos=qos, metadata={"commissioning_test": action, "slave_id": board.slave_id})
            board.last_test_at = datetime.now(timezone.utc)
            board.last_test_result = "CAMERA_ACKNOWLEDGEMENT_PENDING"
            db.commit()
            return len(frames)

    def read_rs485(self, board_id: UUID) -> UUID:
        with self._session_factory() as db:
            board = db.scalar(board_query().where(ControlBoard.id == board_id))
            if board is None:
                raise RuntimeError("Control board not found")
            self._usable(board)
            board.communication_sync_status = "APPLYING"
            board.last_error = None
            db.commit()
            command = self._commands.enqueue_device_setting(
                camera_id=board.camera_id,
                serial_number=board.camera.serial_number,
                command_type="RS485_GET",
                setting_type="evs_get_rs485",
                body=get_rs485_body(board.serial_channel),
                request_metadata={"source": board.serial_channel},
            )
            return command.id

    def sync_rs485(self, board_id: UUID) -> UUID:
        with self._session_factory() as db:
            board = db.scalar(board_query().where(ControlBoard.id == board_id))
            if board is None:
                raise RuntimeError("Control board not found")
            self._usable(board)
            body = set_rs485_body(
                source=board.serial_channel,
                baud_rate=board.baud_rate,
                data_bits=board.data_bits,
                parity=board.parity,
                stop_bits=board.stop_bits,
            )
            board.communication_sync_status = "APPLYING"
            board.last_error = None
            db.commit()
            command = self._commands.enqueue_device_setting(
                camera_id=board.camera_id,
                serial_number=board.camera.serial_number,
                command_type="RS485_SET",
                setting_type="evs_set_rs485",
                body=body,
                request_metadata={"desired": body},
            )
            return command.id

    def send_preview_led(self, board_id: UUID, rows: tuple[str, str, str, str]) -> int:
        with self._session_factory() as db:
            board = db.scalar(board_query().where(ControlBoard.id == board_id))
            if board is None:
                raise RuntimeError("Control board not found")
            self._usable(board)
            for number, text in enumerate(rows, 1):
                self._enqueue(board, "control_board_led_test", idle_frame(number, text, number, address=board.slave_id), qos=1, metadata={"row": number, "commissioning_test": True})
            board.last_test_at = datetime.now(timezone.utc)
            board.last_test_result = "CAMERA_ACKNOWLEDGEMENT_PENDING"
            db.commit()
            return 4

    def _loop(self) -> None:
        while not self._stop.wait(settings.control_board_runtime_interval_seconds):
            try:
                with self._session_factory() as db:
                    for board in db.scalars(board_query()).unique().all():
                        self._apply(db, board)
            except Exception:
                pass

    def _apply(self, db: Session, board: ControlBoard, *, force: bool = False) -> None:
        if not board.enabled or board.camera.connection_state != ONLINE or not self._is_connected():
            board.runtime_status = "UNAVAILABLE"
            self._signatures.pop(board.id, None)
            db.commit()
            return
        state = ControlBoardRuntimeService(db).resolve(board)
        signature = (state.preset_id, state.mode, state.rows, state.traffic_state, state.green_seconds, state.led_brightness, state.led_scroll_speed)
        if not force and self._signatures.get(board.id) == signature:
            return
        try:
            self._enqueue(board, "preset_led_settings", display_settings_frame(state.led_brightness, state.led_scroll_speed, address=board.slave_id), qos=1, metadata={"preset": state.preset_name, "mode": state.mode, "brightness": state.led_brightness, "scroll_speed": state.led_scroll_speed})
            for number, value in enumerate(state.rows, 1):
                self._enqueue(board, "preset_led_row", idle_frame(number, value, number, address=board.slave_id), qos=1, metadata={"preset": state.preset_name, "mode": state.mode, "row": number})
            traffic = traffic_light_frame(red=state.traffic_state == "RED", seconds=state.green_seconds, address=board.slave_id)
            self._enqueue(board, "preset_traffic", traffic, metadata={"preset": state.preset_name, "mode": state.mode, "traffic": state.traffic_state, "timed_green": state.traffic_state == "GREEN"})
            board.runtime_status = "CAMERA_ACKNOWLEDGEMENT_PENDING"
            board.last_applied_at = datetime.now(timezone.utc)
            board.last_error = None
            self._signatures[board.id] = signature
        except (RuntimeError, ValueError, UnicodeError) as error:
            board.runtime_status = "ERROR"
            board.last_error = str(error)[:500]
        db.commit()

    def _usable(self, board: ControlBoard) -> None:
        if not self._is_connected():
            raise RuntimeError("MQTT broker is not connected")
        if not board.enabled:
            raise RuntimeError("Control board is disabled")
        if board.camera.connection_state != ONLINE:
            raise RuntimeError("Camera is offline")
        if not board.camera.serial_number:
            raise RuntimeError("Camera serial number is required")

    def _enqueue(self, board: ControlBoard, command_type: str, frame: bytes, *, qos: int = 0, metadata: dict | None = None) -> None:
        self._commands.enqueue_serial(
            camera_id=board.camera_id,
            serial_number=board.camera.serial_number,
            command_type=command_type,
            raw_frame=frame,
            request_metadata=metadata,
            qos=qos,
            serial_channel=board.serial_channel,
        )
