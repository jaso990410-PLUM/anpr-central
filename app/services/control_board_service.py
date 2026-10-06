from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models.camera import Camera
from app.models.control_board import ControlBoard, PeripheralPreset, PeripheralPresetSchedule
from app.integrations.visionzenith.rs485 import BAUD_RATES, PARITY_TO_ID


class ControlBoardValidationError(ValueError):
    pass


class ControlBoardService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_for_camera(self, camera_id: UUID) -> ControlBoard | None:
        return self.db.scalar(
            select(ControlBoard)
            .options(
                selectinload(ControlBoard.camera).selectinload(Camera.site),
                selectinload(ControlBoard.default_preset),
                selectinload(ControlBoard.schedules).selectinload(PeripheralPresetSchedule.preset),
            )
            .where(ControlBoard.camera_id == camera_id)
        )

    def save(
        self,
        camera: Camera,
        *,
        enabled: bool,
        serial_channel: object,
        baud_rate: object,
        data_bits: object,
        parity: object,
        stop_bits: object,
        slave_id: object,
        default_preset_id: object = None,
    ) -> tuple[ControlBoard, bool]:
        channel = _integer(serial_channel, "Serial channel")
        if channel not in (0, 1):
            raise ControlBoardValidationError("Serial channel must be 0 or 1")
        baud = _positive_integer(baud_rate, "Baud rate")
        if baud not in BAUD_RATES:
            raise ControlBoardValidationError("Select a documented baud rate")
        bits = _positive_integer(data_bits, "Data bits")
        if bits != 8:
            raise ControlBoardValidationError("Data bits must be 8")
        stops = _positive_integer(stop_bits, "Stop bits")
        if stops not in (1, 2):
            raise ControlBoardValidationError("Stop bits must be 1 or 2")
        address = _integer(slave_id, "Slave ID")
        if not 0 <= address <= 255:
            raise ControlBoardValidationError("Slave ID must be 0 to 255")
        parity_value = str(parity or "").strip().upper()
        if parity_value not in PARITY_TO_ID:
            raise ControlBoardValidationError("Parity must be None, Odd, or Even")
        preset = self._preset(default_preset_id)
        board = self.get_for_camera(camera.id)
        created = board is None
        if board is None:
            board = ControlBoard(camera=camera)
            self.db.add(board)
        previous = (board.serial_channel, board.baud_rate, board.data_bits, board.parity, board.stop_bits)
        board.enabled = enabled
        board.serial_channel = channel
        board.baud_rate = baud
        board.data_bits = bits
        board.parity = parity_value
        board.stop_bits = stops
        board.slave_id = address
        board.default_preset = preset
        desired = (channel, baud, bits, parity_value, stops)
        if created or previous != desired:
            board.communication_sync_status = "PENDING_SYNC"
        try:
            self.db.flush()
        except IntegrityError as error:
            raise ControlBoardValidationError("This camera already has a control board") from error
        return board, created

    def add_schedule(
        self,
        board: ControlBoard,
        *,
        preset_id: object,
        starts_at: object,
        ends_at: object,
        enabled: bool = True,
    ) -> PeripheralPresetSchedule:
        preset = self._preset(preset_id)
        if preset is None:
            raise ControlBoardValidationError("Select a preset")
        start = _local_datetime(starts_at, board.camera.site.timezone, "Start")
        end = _local_datetime(ends_at, board.camera.site.timezone, "End")
        if start >= end:
            raise ControlBoardValidationError("Schedule end must be later than its start")
        conflict = self.db.scalar(
            select(PeripheralPresetSchedule.id).where(
                PeripheralPresetSchedule.control_board_id == board.id,
                PeripheralPresetSchedule.enabled.is_(True),
                PeripheralPresetSchedule.starts_at < end,
                PeripheralPresetSchedule.ends_at > start,
            )
        )
        if enabled and conflict is not None:
            raise ControlBoardValidationError("Scheduled presets must not overlap for the same control board")
        schedule = PeripheralPresetSchedule(
            control_board=board,
            preset=preset,
            starts_at=start,
            ends_at=end,
            enabled=enabled,
        )
        self.db.add(schedule)
        self.db.flush()
        return schedule

    def delete_schedule(self, board: ControlBoard, schedule_id: UUID) -> bool:
        schedule = self.db.get(PeripheralPresetSchedule, schedule_id)
        if schedule is None or schedule.control_board_id != board.id:
            return False
        self.db.delete(schedule)
        self.db.flush()
        return True

    def presets(self) -> tuple[PeripheralPreset, ...]:
        return tuple(self.db.scalars(select(PeripheralPreset).order_by(PeripheralPreset.name)).all())

    def _preset(self, value: object) -> PeripheralPreset | None:
        if value in (None, ""):
            return None
        try:
            preset_id = UUID(str(value))
        except ValueError as error:
            raise ControlBoardValidationError("Select a valid preset") from error
        preset = self.db.get(PeripheralPreset, preset_id)
        if preset is None:
            raise ControlBoardValidationError("Select a valid preset")
        return preset


def _integer(value: object, label: str) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError) as error:
        raise ControlBoardValidationError(f"{label} must be an integer") from error


def _positive_integer(value: object, label: str) -> int:
    parsed = _integer(value, label)
    if parsed <= 0:
        raise ControlBoardValidationError(f"{label} must be greater than zero")
    return parsed


def _local_datetime(value: object, timezone_name: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise ControlBoardValidationError(f"{label} is invalid") from error
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(timezone.utc)
