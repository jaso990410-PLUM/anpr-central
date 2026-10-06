from __future__ import annotations

from datetime import time
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.control_board import PeripheralBehavior, PeripheralBusyPeriod, PeripheralLedRow, PeripheralPreset


MODES = ("IDLE", "BUSY")
STANDARD_SOURCES = {
    "BLANK", "RECOGNIZED_PLATE", "ACCESS_STATUS", "CAMERA_NAME",
    "SITE_NAME", "CAMERA_ROLE", "CURRENT_DATE", "CURRENT_TIME",
}
VOICES = {3, 51, 52, 53, 54, 55}


class PeripheralPresetValidationError(ValueError):
    pass


class PeripheralPresetService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list(self) -> tuple[PeripheralPreset, ...]:
        return tuple(self.db.scalars(select(PeripheralPreset).order_by(PeripheralPreset.name)).all())

    def get(self, preset_id: UUID) -> PeripheralPreset | None:
        return self.db.scalar(
            select(PeripheralPreset)
            .options(
                selectinload(PeripheralPreset.behaviors).selectinload(PeripheralBehavior.led_rows),
                selectinload(PeripheralPreset.busy_periods),
            )
            .where(PeripheralPreset.id == preset_id)
        )

    def create(self, *, name: object, description: object = None, enabled: bool = True) -> PeripheralPreset:
        clean_name = self._name(name)
        if self.db.scalar(select(PeripheralPreset.id).where(PeripheralPreset.name == clean_name)):
            raise PeripheralPresetValidationError("A preset with that name already exists")
        preset = PeripheralPreset(name=clean_name, description=_optional(description, 500), enabled=enabled)
        self.db.add(preset)
        for mode in MODES:
            behavior = PeripheralBehavior(preset=preset, mode=mode, traffic_state="RED")
            self.db.add(behavior)
            for row in range(1, 5):
                self.db.add(PeripheralLedRow(behavior=behavior, row_number=row, content_type="STANDARD", standard_source="BLANK"))
        self.db.flush()
        return preset

    def update(self, preset: PeripheralPreset, values: dict[str, object]) -> None:
        clean_name = self._name(values.get("name"))
        duplicate = self.db.scalar(select(PeripheralPreset.id).where(PeripheralPreset.name == clean_name, PeripheralPreset.id != preset.id))
        if duplicate:
            raise PeripheralPresetValidationError("A preset with that name already exists")
        preset.name = clean_name
        preset.description = _optional(values.get("description"), 500)
        preset.enabled = _bool(values.get("enabled"))
        behaviors = {item.mode: item for item in preset.behaviors}
        for mode in MODES:
            key = mode.lower()
            behavior = behaviors[mode]
            traffic = str(values.get(f"{key}_traffic_state", "RED")).upper()
            if traffic not in {"RED", "GREEN"}:
                raise PeripheralPresetValidationError("Traffic state must be Red or Green")
            behavior.traffic_state = traffic
            behavior.traffic_green_seconds = _range(values.get(f"{key}_green_seconds", 5), 1, 255, "Green duration")
            behavior.led_brightness = _range(values.get(f"{key}_led_brightness", 5), 0, 5, "LED brightness")
            behavior.led_scroll_speed = _range(values.get(f"{key}_led_scroll_speed", 5), 0, 5, "LED scroll speed")
            behavior.speaker_enabled = _bool(values.get(f"{key}_speaker_enabled"))
            behavior.speaker_volume = _range(values.get(f"{key}_speaker_volume", 7), 0, 10, "Volume")
            behavior.speaker_speed = _range(values.get(f"{key}_speaker_speed", 5), 0, 10, "Speed")
            behavior.speaker_pitch = _range(values.get(f"{key}_speaker_pitch", 5), 0, 10, "Pitch")
            behavior.speaker_voice = _range(values.get(f"{key}_speaker_voice", 3), 0, 99, "Voice")
            if behavior.speaker_voice not in VOICES:
                raise PeripheralPresetValidationError("Voice must be one of 3, 51, 52, 53, 54, 55")
            behavior.plate_pause_ms = _range(values.get(f"{key}_plate_pause_ms", 100), 0, 9999, "Plate pause")
            behavior.speaker_message = str(values.get(f"{key}_speaker_message", "")).strip()
            if len(behavior.speaker_message.encode("gbk")) > 200:
                raise PeripheralPresetValidationError("Speaker message is too long after GBK encoding")
            rows = {row.row_number: row for row in behavior.led_rows}
            for number in range(1, 5):
                row = rows[number]
                content_type = str(values.get(f"{key}_row{number}_type", "STANDARD")).upper()
                source = str(values.get(f"{key}_row{number}_source", "BLANK")).upper()
                custom = str(values.get(f"{key}_row{number}_text", ""))
                if content_type not in {"STANDARD", "CUSTOM"}:
                    raise PeripheralPresetValidationError("LED row type must be Standard or Custom")
                if source not in STANDARD_SOURCES:
                    raise PeripheralPresetValidationError("Unsupported standard LED source")
                if any(ord(character) < 32 for character in custom) or len(custom.encode("gbk")) > 255:
                    raise PeripheralPresetValidationError("LED text must be one line and at most 255 GBK bytes")
                row.content_type = content_type
                row.standard_source = source
                row.custom_text = custom or None
        periods = parse_busy_periods(values.get("busy_periods"))
        preset.busy_periods.clear()
        preset.busy_periods.extend(PeripheralBusyPeriod(day_of_week=day, start_time=start, end_time=end) for day, start, end in periods)
        self.db.flush()

    @staticmethod
    def _name(value: object) -> str:
        name = str(value or "").strip()
        if not name or len(name) > 100:
            raise PeripheralPresetValidationError("Preset name is required and must contain at most 100 characters")
        return name


def parse_busy_periods(value: object) -> tuple[tuple[int, time, time], ...]:
    periods: list[tuple[int, time, time]] = []
    for line_number, raw in enumerate(str(value or "").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            day_text, start_text, end_text = [part.strip() for part in line.split(",")]
            day = int(day_text)
            start, end = time.fromisoformat(start_text), time.fromisoformat(end_text)
        except (ValueError, TypeError) as error:
            raise PeripheralPresetValidationError(f"Busy period line {line_number} must be day,start,end") from error
        if not 0 <= day <= 6 or start >= end:
            raise PeripheralPresetValidationError(f"Busy period line {line_number} has an invalid day or range")
        for other_day, other_start, other_end in periods:
            if day == other_day and start < other_end and end > other_start:
                raise PeripheralPresetValidationError("Busy periods must not overlap")
        periods.append((day, start, end))
    return tuple(periods)


def _bool(value: object) -> bool:
    return value is True or str(value).lower() in {"true", "1", "on"}


def _range(value: object, minimum: int, maximum: int, label: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as error:
        raise PeripheralPresetValidationError(f"{label} must be an integer") from error
    if not minimum <= parsed <= maximum:
        raise PeripheralPresetValidationError(f"{label} must be {minimum} to {maximum}")
    return parsed


def _optional(value: object, maximum: int) -> str | None:
    clean = str(value or "").strip()
    if len(clean) > maximum:
        raise PeripheralPresetValidationError(f"Description must contain at most {maximum} characters")
    return clean or None
