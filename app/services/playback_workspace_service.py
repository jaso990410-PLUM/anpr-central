from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models.camera import Camera
from app.models.media import MediaSource, RecordingSegment
from app.models.parking import AccessDecision
from app.models.recognition_event import RecognitionEvent
from app.models.site import Site
from app.services.plate_correction_service import effective_plate


class PlaybackWorkspaceError(ValueError):
    pass


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def site_zone(site: Site) -> ZoneInfo:
    try:
        return ZoneInfo(site.timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def local_day_bounds(day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    return start, end


def merge_coverage(
    segments: tuple[RecordingSegment, ...], start: datetime, end: datetime
) -> tuple[dict[str, object], ...]:
    merged: list[list[datetime]] = []
    for segment in segments:
        if segment.ended_at is None:
            continue
        segment_start = max(start, utc(segment.started_at))
        segment_end = min(end, utc(segment.ended_at))
        if segment_end <= segment_start:
            continue
        if merged and segment_start <= merged[-1][1] + timedelta(seconds=1):
            merged[-1][1] = max(merged[-1][1], segment_end)
        else:
            merged.append([segment_start, segment_end])
    day_seconds = max(1.0, (end - start).total_seconds())
    return tuple(
        {
            "start": range_start.isoformat().replace("+00:00", "Z"),
            "end": range_end.isoformat().replace("+00:00", "Z"),
            "start_percent": (range_start - start).total_seconds() / day_seconds * 100,
            "width_percent": (range_end - range_start).total_seconds() / day_seconds * 100,
        }
        for range_start, range_end in merged
    )


class PlaybackWorkspaceService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def sites(self) -> tuple[Site, ...]:
        return tuple(self.db.scalars(select(Site).where(Site.is_active.is_(True)).order_by(Site.code)).all())

    def cameras(self, site_id: UUID) -> tuple[Camera, ...]:
        return tuple(
            self.db.scalars(
                select(Camera)
                .join(MediaSource, MediaSource.camera_id == Camera.id)
                .options(joinedload(Camera.site))
                .where(
                    Camera.site_id == site_id,
                    Camera.is_enabled.is_(True),
                    MediaSource.enabled.is_(True),
                    MediaSource.recording_enabled.is_(True),
                    MediaSource.provider_type == "CENTRAL_RECORDER",
                )
                .order_by(Camera.code)
            ).all()
        )

    def coverage(self, camera_id: UUID, start: datetime, end: datetime) -> tuple[dict[str, object], ...]:
        segments = tuple(
            self.db.scalars(
                select(RecordingSegment)
                .where(
                    RecordingSegment.camera_id == camera_id,
                    RecordingSegment.state == "COMPLETE",
                    RecordingSegment.started_at < end,
                    RecordingSegment.ended_at > start,
                )
                .order_by(RecordingSegment.started_at)
            ).all()
        )
        return merge_coverage(segments, start, end)

    def calendar(self, site: Site, selected: date) -> tuple[tuple[dict[str, object], ...], ...]:
        zone = site_zone(site)
        first = selected.replace(day=1)
        next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        month_start, _ = local_day_bounds(first, zone)
        month_end, _ = local_day_bounds(next_month, zone)
        ranges = self.db.execute(
            select(RecordingSegment.started_at, RecordingSegment.ended_at)
            .join(Camera, Camera.id == RecordingSegment.camera_id)
            .where(
                Camera.site_id == site.id,
                RecordingSegment.state == "COMPLETE",
                RecordingSegment.started_at < month_end,
                RecordingSegment.ended_at > month_start,
            )
        ).all()
        available: set[date] = set()
        for started_at, ended_at in ranges:
            current = max(first, utc(started_at).astimezone(zone).date())
            last = min(
                next_month - timedelta(days=1),
                (utc(ended_at) - timedelta(microseconds=1)).astimezone(zone).date(),
            )
            while current <= last:
                available.add(current)
                current += timedelta(days=1)
        weeks = calendar.Calendar(firstweekday=6).monthdatescalendar(selected.year, selected.month)
        return tuple(
            tuple(
                {
                    "date": day.isoformat(),
                    "day": day.day,
                    "in_month": day.month == selected.month,
                    "selected": day == selected,
                    "available": day in available,
                }
                for day in week
            )
            for week in weeks
        )

    def markers(
        self, site_id: UUID, camera_ids: tuple[UUID, ...], start: datetime, end: datetime, zone: ZoneInfo
    ) -> tuple[dict[str, object], ...]:
        if not camera_ids:
            return ()
        occurred = func.coalesce(RecognitionEvent.captured_at, RecognitionEvent.received_at)
        events = tuple(
            self.db.scalars(
                select(RecognitionEvent)
                .options(joinedload(RecognitionEvent.camera))
                .where(
                    RecognitionEvent.site_id == site_id,
                    RecognitionEvent.camera_id.in_(camera_ids),
                    occurred >= start,
                    occurred < end,
                )
                .order_by(occurred)
                .limit(3000)
            ).all()
        )
        decisions = {
            decision.recognition_event_id: decision
            for decision in self.db.scalars(
                select(AccessDecision).where(AccessDecision.recognition_event_id.in_([event.id for event in events]))
            ).all()
        } if events else {}
        offsets = {
            camera_id: offset or 0
            for camera_id, offset in self.db.execute(
                select(MediaSource.camera_id, MediaSource.media_time_offset_ms).where(MediaSource.camera_id.in_(camera_ids))
            ).all()
        }
        day_seconds = max(1.0, (end - start).total_seconds())
        result = []
        for event in events:
            occurred_at = utc(event.captured_at or event.received_at)
            media_at = occurred_at + timedelta(milliseconds=offsets.get(event.camera_id, 0))
            plate = effective_plate(self.db, event)
            decision = decisions.get(event.id)
            result.append({
                "id": str(event.id),
                "camera_id": str(event.camera_id),
                "camera": event.camera.name,
                "camera_code": event.camera.code,
                "plate": plate,
                "raw_plate": event.plate_text,
                "corrected": plate != event.plate_text,
                "at": media_at.isoformat().replace("+00:00", "Z"),
                "recognition_at": occurred_at.isoformat().replace("+00:00", "Z"),
                "local_time": occurred_at.astimezone(zone).strftime("%H:%M:%S"),
                "offset_percent": (media_at - start).total_seconds() / day_seconds * 100,
                "decision": decision.decision if decision else "NOT EVALUATED",
                "source": event.source,
                "clip_url": f"/media/events/{event.id}",
            })
        return tuple(result)

