from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.media import MediaSource, RecordingSegment


@dataclass(frozen=True)
class MediaAvailability:
    status: str
    segments: tuple[RecordingSegment, ...]
    covered_seconds: float
    requested_seconds: float


class VideoProvider(ABC):
    @abstractmethod
    def get_recording_availability(self, camera_id: UUID, start_at: datetime, end_at: datetime) -> MediaAvailability: ...

    @abstractmethod
    def playback_mode(self, segments: tuple[RecordingSegment, ...]) -> str: ...


class CentralRecorderProvider(VideoProvider):
    def __init__(self, db: Session): self.db = db

    def get_recording_availability(self, camera_id: UUID, start_at: datetime, end_at: datetime) -> MediaAvailability:
        start, end = _utc(start_at), _utc(end_at)
        if end <= start: raise ValueError("Media window end must be after start")
        segments = tuple(self.db.scalars(select(RecordingSegment).where(
            RecordingSegment.camera_id == camera_id,
            RecordingSegment.state == "COMPLETE",
            RecordingSegment.started_at < end,
            RecordingSegment.ended_at > start,
        ).order_by(RecordingSegment.started_at)).all())
        intervals = sorted((max(start, _utc(s.started_at)), min(end, _utc(s.ended_at))) for s in segments if s.ended_at)
        covered = 0.0; cursor = start
        for left, right in intervals:
            if right <= cursor: continue
            if left > cursor: cursor = left
            covered += max(0.0, (right - cursor).total_seconds()); cursor = max(cursor, right)
        requested = (end - start).total_seconds()
        status = "UNAVAILABLE" if covered <= 0 else ("AVAILABLE" if covered >= requested - 0.25 else "PARTIAL")
        return MediaAvailability(status, segments, covered, requested)

    def playback_mode(self, segments: tuple[RecordingSegment, ...]) -> str:
        codecs = {str(s.codec or "").lower() for s in segments}
        return "DIRECT" if len(segments) == 1 and codecs <= {"h264", "avc1", ""} else ("REMUX" if codecs <= {"h264", "avc1", ""} else "ON_DEMAND_TRANSCODE")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
