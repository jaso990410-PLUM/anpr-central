from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.models.camera import Camera
from app.models.media import EventMediaReference, MediaEvidenceHold, MediaExport, MediaSource, MediaStorage, RecordingSegment
from app.models.recognition_event import RecognitionEvent
from app.services.media_provider import CentralRecorderProvider, MediaAvailability


class MediaValidationError(ValueError): pass


def safe_stream_label(uri: str | None) -> str:
    if not uri: return "Not configured"
    parts = urlsplit(uri)
    host = parts.hostname or "configured source"
    return f"{parts.scheme.upper()} stream at {host}"


def safe_stream_host(uri: str | None) -> str | None:
    """Return only the non-sensitive host portion of a configured stream URI."""
    if not uri:
        return None
    return urlsplit(uri).hostname


def humanize_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if amount < 1024 or unit == "PB":
            precision = 0 if unit == "B" else 1
            return f"{amount:.{precision}f} {unit}"
        amount /= 1024
    return "0 B"


def validate_stream_uri(value: str | None) -> str | None:
    uri = str(value or "").strip()
    if not uri: return None
    parts = urlsplit(uri)
    if parts.scheme.lower() != "rtsp" or not parts.hostname:
        raise MediaValidationError("Recording stream must be a valid RTSP URI")
    if parts.username or parts.password:
        raise MediaValidationError("Do not embed credentials in the RTSP URI; use a credential reference")
    return uri


class MediaConfigurationService:
    def __init__(self, db: Session): self.db = db

    def source_for_camera(self, camera_id: UUID) -> MediaSource | None:
        return self.db.scalar(select(MediaSource).options(joinedload(MediaSource.storage)).where(MediaSource.camera_id == camera_id))

    def save_source(self, camera: Camera, *, provider_type: str, enabled: bool, recording_enabled: bool,
                    recording_stream_uri: str | None, live_stream_uri: str | None, credential_reference: str | None,
                    storage_id: UUID | None, pre_event_seconds: int, post_event_seconds: int,
                    segment_duration_seconds: int, media_time_offset_ms: int, expected_codec: str | None) -> MediaSource:
        if provider_type not in {"NONE","CENTRAL_RECORDER","EXTERNAL_NVR"}: raise MediaValidationError("Select a valid recording provider")
        if not 0 <= pre_event_seconds <= 60 or not 0 <= post_event_seconds <= 120: raise MediaValidationError("Event video timing is outside the safe range")
        if not 30 <= segment_duration_seconds <= 300: raise MediaValidationError("Segment duration must be 30 to 300 seconds")
        if not -300000 <= media_time_offset_ms <= 300000: raise MediaValidationError("Media time offset must be within five minutes")
        recording_uri = validate_stream_uri(recording_stream_uri); live_uri = validate_stream_uri(live_stream_uri)
        credential = str(credential_reference or "").strip().upper() or None
        if credential and not re.fullmatch(r"[A-Z0-9_]{1,120}", credential): raise MediaValidationError("Credential reference may contain only letters, numbers and underscores")
        storage = self.db.get(MediaStorage, storage_id) if storage_id else None
        if provider_type == "CENTRAL_RECORDER" and recording_enabled and (not recording_uri or storage is None or not storage.enabled):
            raise MediaValidationError("Central recording requires an RTSP stream and enabled storage")
        source = self.source_for_camera(camera.id) or MediaSource(camera_id=camera.id)
        source.provider_type=provider_type; source.enabled=enabled; source.recording_enabled=recording_enabled and enabled
        source.recording_stream_uri=recording_uri; source.live_stream_uri=live_uri; source.credential_reference=credential
        source.storage_id=storage_id; source.pre_event_seconds=pre_event_seconds; source.post_event_seconds=post_event_seconds
        source.segment_duration_seconds=segment_duration_seconds; source.media_time_offset_ms=media_time_offset_ms
        source.expected_codec=str(expected_codec or "").strip().lower() or None
        if not source.recording_enabled: source.recorder_state="DISABLED"
        self.db.add(source); self.db.flush(); return source

    def save_storage(self, *, storage_id: UUID | None, name: str, storage_type: str, root_path: str,
                     enabled: bool, retention_days: int | None, minimum_free_space_gb: int,
                     minimum_free_space_percent: int) -> MediaStorage:
        if storage_type not in {"LOCAL","NETWORK"}: raise MediaValidationError("Select Local or Network storage")
        if not name.strip(): raise MediaValidationError("Storage name is required")
        root = Path(root_path).expanduser()
        if not root.is_absolute(): root = (Path.cwd() / root).resolve()
        if retention_days is not None and not 1 <= retention_days <= 3650: raise MediaValidationError("Retention must be 1 to 3650 days")
        if minimum_free_space_gb < 0 or not 0 <= minimum_free_space_percent <= 95: raise MediaValidationError("Invalid free-space threshold")
        item = self.db.get(MediaStorage, storage_id) if storage_id else MediaStorage()
        if item is None: raise MediaValidationError("Storage target not found")
        item.name=name.strip(); item.storage_type=storage_type; item.root_path=str(root); item.enabled=enabled
        item.retention_days=retention_days; item.minimum_free_space_gb=minimum_free_space_gb; item.minimum_free_space_percent=minimum_free_space_percent
        self.db.add(item); self.db.flush(); return item

    def storages(self): return tuple(self.db.scalars(select(MediaStorage).order_by(MediaStorage.name)).all())


class EventMediaService:
    def __init__(self, db: Session): self.db = db

    def ensure_reference(self, event_id: UUID) -> EventMediaReference | None:
        existing = self.db.scalar(select(EventMediaReference).where(EventMediaReference.recognition_event_id == event_id))
        if existing: return existing
        event = self.db.get(RecognitionEvent, event_id)
        if event is None: return None
        source = self.db.scalar(select(MediaSource).where(MediaSource.camera_id == event.camera_id))
        base = _utc(event.captured_at or event.received_at)
        if source is None or not source.enabled or source.provider_type == "NONE" or not source.recording_enabled:
            item = EventMediaReference(recognition_event_id=event.id, camera_id=event.camera_id,
                media_source_id=source.id if source else None, media_anchor_at=base, clip_start_at=base, clip_end_at=base, status="NOT_CONFIGURED")
        else:
            anchor = base + timedelta(milliseconds=source.media_time_offset_ms)
            item = EventMediaReference(recognition_event_id=event.id, camera_id=event.camera_id, media_source_id=source.id,
                media_anchor_at=anchor, clip_start_at=anchor-timedelta(seconds=source.pre_event_seconds),
                clip_end_at=anchor+timedelta(seconds=source.post_event_seconds), status="PENDING")
        self.db.add(item); self.db.flush(); return item

    def resolve(self, reference: EventMediaReference, *, now: datetime | None = None) -> MediaAvailability:
        if reference.status == "NOT_CONFIGURED" or reference.media_source_id is None:
            return MediaAvailability("NOT_CONFIGURED", (), 0, 0)
        source = self.db.get(MediaSource, reference.media_source_id)
        if source is None or not source.enabled or not source.recording_enabled:
            reference.status="NOT_CONFIGURED"; self.db.flush(); return MediaAvailability("NOT_CONFIGURED", (), 0, 0)
        now = _utc(now or datetime.now(timezone.utc))
        if now < _utc(reference.clip_end_at):
            reference.status="PENDING"; return MediaAvailability("PENDING", (), 0, max(0, (_utc(reference.clip_end_at)-_utc(reference.clip_start_at)).total_seconds()))
        if source.provider_type != "CENTRAL_RECORDER":
            reference.status="UNAVAILABLE"; self.db.flush(); return MediaAvailability("UNAVAILABLE", (), 0, 0)
        availability = CentralRecorderProvider(self.db).get_recording_availability(reference.camera_id, reference.clip_start_at, reference.clip_end_at)
        if availability.status == "UNAVAILABLE" and source.recorder_state in {"DISCONNECTED", "ERROR", "DEGRADED"}:
            reference.status="RECORDER_OFFLINE"
            availability=MediaAvailability("RECORDER_OFFLINE",availability.segments,availability.covered_seconds,availability.requested_seconds)
        else:
            reference.status=availability.status
        self.db.flush(); return availability

    def for_event(self, event_id: UUID, *, resolve: bool = True):
        ref = self.ensure_reference(event_id)
        if ref and resolve: self.resolve(ref)
        return ref


class MediaPlaybackService:
    def __init__(self, db: Session): self.db=db

    def live_sources(self) -> tuple[MediaSource, ...]:
        return tuple(
            self.db.scalars(
                select(MediaSource)
                .options(joinedload(MediaSource.camera))
                .join(MediaSource.camera)
                .where(
                    MediaSource.enabled.is_(True),
                    MediaSource.provider_type == "CENTRAL_RECORDER",
                    (MediaSource.live_stream_uri.is_not(None))
                    | (MediaSource.recording_stream_uri.is_not(None)),
                )
                .order_by(Camera.code)
            ).all()
        )

    def event_playback(self, event_id: UUID):
        ref = EventMediaService(self.db).for_event(event_id)
        if ref is None: raise MediaValidationError("Recognition event not found")
        availability = EventMediaService(self.db).resolve(ref)
        mode = CentralRecorderProvider(self.db).playback_mode(availability.segments) if availability.segments else None
        return ref, availability, mode

    def historical(self, camera_id: UUID, start_at: datetime, end_at: datetime):
        if _utc(end_at) - _utc(start_at) > timedelta(minutes=settings.media_max_playback_minutes):
            raise MediaValidationError(f"Playback range is limited to {settings.media_max_playback_minutes} minutes")
        return CentralRecorderProvider(self.db).get_recording_availability(camera_id, start_at, end_at)

    def live_source(self, camera_id: UUID) -> MediaSource:
        source = self.db.scalar(select(MediaSource).where(MediaSource.camera_id == camera_id))
        if source is None or not source.enabled or source.provider_type != "CENTRAL_RECORDER":
            raise MediaValidationError("Live video is not configured for this camera")
        if not (source.live_stream_uri or source.recording_stream_uri):
            raise MediaValidationError("Live RTSP stream is not configured")
        return source

    @staticmethod
    def segment_path(segment: RecordingSegment, storage: MediaStorage) -> Path:
        root = Path(storage.root_path).resolve(); path = (root / segment.relative_path).resolve()
        if root != path and root not in path.parents: raise MediaValidationError("Invalid media path")
        if not path.is_file(): raise MediaValidationError("Recording file is unavailable")
        return path


class MediaEvidenceService:
    def __init__(self, db: Session): self.db=db

    def protect(self, *, camera_id: UUID, start_at: datetime, end_at: datetime, reason: str, user_id: UUID, event_id: UUID | None = None):
        if not reason.strip(): raise MediaValidationError("Evidence hold reason is required")
        if _utc(end_at) <= _utc(start_at): raise MediaValidationError("Evidence hold window is invalid")
        hold=MediaEvidenceHold(camera_id=camera_id, recognition_event_id=event_id, start_at=_utc(start_at), end_at=_utc(end_at), reason=reason.strip()[:500], created_by_user_id=user_id)
        self.db.add(hold); self.db.flush(); return hold

    def release(self, hold: MediaEvidenceHold, user_id: UUID):
        if hold.released_at is None: hold.released_at=datetime.now(timezone.utc); hold.released_by_user_id=user_id; self.db.flush()

    def request_export(self, *, camera_id: UUID, start_at: datetime, end_at: datetime, user_id: UUID, event_id: UUID | None=None):
        if _utc(end_at) <= _utc(start_at) or _utc(end_at)-_utc(start_at) > timedelta(minutes=settings.media_max_playback_minutes): raise MediaValidationError("Export range is invalid or too long")
        item=MediaExport(requested_by_user_id=user_id,camera_id=camera_id,recognition_event_id=event_id,start_at=_utc(start_at),end_at=_utc(end_at),state="QUEUED")
        self.db.add(item); self.db.flush(); return item


def storage_health(storage: MediaStorage) -> dict[str, object]:
    root=Path(storage.root_path)
    try:
        root.mkdir(parents=True,exist_ok=True); usage=shutil.disk_usage(root)
        writable=root.is_dir() and __import__('os').access(root, __import__('os').W_OK)
        free_gb=usage.free/(1024**3); free_pct=usage.free*100/usage.total if usage.total else 0
        healthy=writable and free_gb >= storage.minimum_free_space_gb and free_pct >= storage.minimum_free_space_percent
        return {"status":"HEALTHY" if healthy else "DEGRADED","writable":writable,"total_bytes":usage.total,"free_bytes":usage.free,"used_bytes":usage.used,"free_percent":round(free_pct,1)}
    except OSError as error: return {"status":"ERROR","writable":False,"error":str(error),"total_bytes":0,"free_bytes":0,"used_bytes":0,"free_percent":0}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
