from __future__ import annotations

import uuid
from datetime import datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, Time, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.camera import Camera


class PeripheralPreset(Base):
    __tablename__ = "peripheral_presets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    behaviors: Mapped[list["PeripheralBehavior"]] = relationship(back_populates="preset", cascade="all, delete-orphan")
    busy_periods: Mapped[list["PeripheralBusyPeriod"]] = relationship(back_populates="preset", cascade="all, delete-orphan")


class ControlBoard(Base):
    __tablename__ = "control_boards"
    __table_args__ = (CheckConstraint("serial_channel IN (0, 1)", name="ck_control_board_serial_channel"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    camera_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    serial_channel: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    baud_rate: Mapped[int] = mapped_column(Integer, default=9600, server_default=text("9600"), nullable=False)
    data_bits: Mapped[int] = mapped_column(Integer, default=8, server_default=text("8"), nullable=False)
    parity: Mapped[str] = mapped_column(String(16), default="NONE", server_default=text("'NONE'"), nullable=False)
    stop_bits: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    slave_id: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    default_preset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("peripheral_presets.id", ondelete="SET NULL"))
    communication_sync_status: Mapped[str] = mapped_column(String(32), default="UNSYNCED", server_default=text("'UNSYNCED'"), nullable=False)
    observed_serial_channel: Mapped[int | None] = mapped_column(Integer)
    observed_baud_rate: Mapped[int | None] = mapped_column(Integer)
    observed_data_bits: Mapped[int | None] = mapped_column(Integer)
    observed_parity_id: Mapped[int | None] = mapped_column(Integer)
    observed_stop_bits: Mapped[int | None] = mapped_column(Integer)
    last_communication_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_result: Mapped[str | None] = mapped_column(String(64))
    runtime_status: Mapped[str] = mapped_column(String(32), default="PENDING", server_default=text("'PENDING'"), nullable=False)
    last_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    camera: Mapped["Camera"] = relationship(back_populates="control_board")
    default_preset: Mapped[PeripheralPreset | None] = relationship(foreign_keys=[default_preset_id])
    schedules: Mapped[list["PeripheralPresetSchedule"]] = relationship(back_populates="control_board", cascade="all, delete-orphan")


class PeripheralBehavior(Base):
    __tablename__ = "peripheral_behaviors"
    __table_args__ = (
        UniqueConstraint("preset_id", "mode", name="uq_peripheral_behavior_preset_mode"),
        CheckConstraint("mode IN ('IDLE', 'BUSY')", name="ck_peripheral_behavior_mode"),
        CheckConstraint("traffic_state IN ('RED', 'GREEN')", name="ck_peripheral_behavior_traffic"),
        CheckConstraint("led_brightness BETWEEN 0 AND 5", name="ck_peripheral_behavior_led_brightness"),
        CheckConstraint("led_scroll_speed BETWEEN 0 AND 5", name="ck_peripheral_behavior_led_scroll_speed"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    preset_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("peripheral_presets.id", ondelete="CASCADE"), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    traffic_state: Mapped[str] = mapped_column(String(8), default="RED", server_default=text("'RED'"), nullable=False)
    traffic_green_seconds: Mapped[int] = mapped_column(Integer, default=5, server_default=text("5"), nullable=False)
    led_brightness: Mapped[int] = mapped_column(Integer, default=5, server_default=text("5"), nullable=False)
    led_scroll_speed: Mapped[int] = mapped_column(Integer, default=5, server_default=text("5"), nullable=False)
    speaker_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    speaker_volume: Mapped[int] = mapped_column(Integer, default=7, server_default=text("7"), nullable=False)
    speaker_speed: Mapped[int] = mapped_column(Integer, default=5, server_default=text("5"), nullable=False)
    speaker_voice: Mapped[int] = mapped_column(Integer, default=3, server_default=text("3"), nullable=False)
    speaker_pitch: Mapped[int] = mapped_column(Integer, default=5, server_default=text("5"), nullable=False)
    speaker_message: Mapped[str] = mapped_column(String(250), default="Welcome {plate}", server_default=text("'Welcome {plate}'"), nullable=False)
    plate_pause_ms: Mapped[int] = mapped_column(Integer, default=100, server_default=text("100"), nullable=False)

    preset: Mapped[PeripheralPreset] = relationship(back_populates="behaviors")
    led_rows: Mapped[list["PeripheralLedRow"]] = relationship(back_populates="behavior", cascade="all, delete-orphan")


class PeripheralLedRow(Base):
    __tablename__ = "peripheral_led_rows"
    __table_args__ = (
        UniqueConstraint("behavior_id", "row_number", name="uq_peripheral_led_behavior_row"),
        CheckConstraint("row_number BETWEEN 1 AND 4", name="ck_peripheral_led_row_number"),
        CheckConstraint("content_type IN ('STANDARD', 'CUSTOM')", name="ck_peripheral_led_content_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    behavior_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("peripheral_behaviors.id", ondelete="CASCADE"), nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str] = mapped_column(String(16), default="STANDARD", server_default=text("'STANDARD'"), nullable=False)
    standard_source: Mapped[str] = mapped_column(String(32), default="BLANK", server_default=text("'BLANK'"), nullable=False)
    custom_text: Mapped[str | None] = mapped_column(String(255))

    behavior: Mapped[PeripheralBehavior] = relationship(back_populates="led_rows")


class PeripheralBusyPeriod(Base):
    __tablename__ = "peripheral_busy_periods"
    __table_args__ = (CheckConstraint("day_of_week BETWEEN 0 AND 6", name="ck_peripheral_busy_day"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    preset_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("peripheral_presets.id", ondelete="CASCADE"), nullable=False, index=True)
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[time] = mapped_column(Time(), nullable=False)
    end_time: Mapped[time] = mapped_column(Time(), nullable=False)

    preset: Mapped[PeripheralPreset] = relationship(back_populates="busy_periods")


class PeripheralPresetSchedule(Base):
    __tablename__ = "peripheral_preset_schedules"
    __table_args__ = (Index("ix_peripheral_schedule_board_window", "control_board_id", "starts_at", "ends_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    control_board_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("control_boards.id", ondelete="CASCADE"), nullable=False)
    preset_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("peripheral_presets.id", ondelete="CASCADE"), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)

    control_board: Mapped[ControlBoard] = relationship(back_populates="schedules")
    preset: Mapped[PeripheralPreset] = relationship()
