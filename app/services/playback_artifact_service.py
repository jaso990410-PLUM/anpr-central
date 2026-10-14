from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
from threading import Lock
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.media import MediaSource, MediaStorage, RecordingSegment
from app.services.media_provider import MediaAvailability
from app.services.media_service import MediaPlaybackService


class PlaybackArtifactError(RuntimeError):
    pass


_locks_guard = Lock()
_artifact_locks: dict[str, Lock] = {}


def _artifact_lock(key: str) -> Lock:
    with _locks_guard:
        return _artifact_locks.setdefault(key, Lock())


class EventPlaybackArtifactService:
    """Build short-lived, finalized MP4 files for seekable event playback."""

    def __init__(self, db: Session):
        self.db = db

    def prepare(
        self,
        *,
        event_id: UUID,
        camera_id: UUID,
        start_at: datetime,
        end_at: datetime,
        availability: MediaAvailability,
    ) -> Path:
        if availability.status not in {"AVAILABLE", "PARTIAL"} or not availability.segments:
            raise PlaybackArtifactError("Event recording is unavailable")

        source = self.db.scalar(select(MediaSource).where(MediaSource.camera_id == camera_id))
        storage = self.db.get(MediaStorage, source.storage_id) if source else None
        if source is None or storage is None:
            raise PlaybackArtifactError("Recording source is unavailable")

        root = Path(storage.root_path).resolve()
        cache_dir = (root / ".playback-cache").resolve()
        if root != cache_dir and root not in cache_dir.parents:
            raise PlaybackArtifactError("Playback cache path is invalid")
        cache_dir.mkdir(parents=True, exist_ok=True)

        cache_key = self._cache_key(event_id, start_at, end_at, availability.segments)
        output = cache_dir / f"event_{event_id}_{cache_key}.mp4"
        lifetime = timedelta(minutes=settings.media_playback_cache_minutes)

        with _artifact_lock(str(output)):
            if self._is_fresh(output, lifetime):
                return output
            output.unlink(missing_ok=True)
            self._remove_expired(cache_dir, lifetime)
            self._generate(
                source=source,
                storage=storage,
                segments=availability.segments,
                start_at=start_at,
                end_at=end_at,
                output=output,
            )
        return output

    @staticmethod
    def _cache_key(
        event_id: UUID,
        start_at: datetime,
        end_at: datetime,
        segments: tuple[RecordingSegment, ...],
    ) -> str:
        material = "|".join(
            (
                str(event_id),
                _utc(start_at).isoformat(),
                _utc(end_at).isoformat(),
                *(f"{item.id}:{item.relative_path}:{item.file_size_bytes or 0}" for item in segments),
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _is_fresh(path: Path, lifetime: timedelta) -> bool:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        return datetime.now(timezone.utc) - modified <= lifetime

    @staticmethod
    def _remove_expired(cache_dir: Path, lifetime: timedelta) -> None:
        cutoff = datetime.now(timezone.utc) - lifetime
        for item in cache_dir.glob("event_*.mp4"):
            try:
                modified = datetime.fromtimestamp(item.stat().st_mtime, timezone.utc)
                if modified < cutoff:
                    item.unlink(missing_ok=True)
            except OSError:
                continue

    def _generate(
        self,
        *,
        source: MediaSource,
        storage: MediaStorage,
        segments: tuple[RecordingSegment, ...],
        start_at: datetime,
        end_at: datetime,
        output: Path,
    ) -> None:
        executable = shutil.which(settings.media_ffmpeg_path)
        if executable is None:
            raise PlaybackArtifactError(
                "Playback preparation is unavailable because ffmpeg is not installed or configured"
            )

        inputs = [MediaPlaybackService.segment_path(item, storage) for item in segments]
        first_start = _utc(segments[0].started_at)
        offset = max(0.0, (_utc(start_at) - first_start).total_seconds())
        duration = max(0.001, (_utc(end_at) - _utc(start_at)).total_seconds())
        token = uuid4().hex
        temporary = output.with_name(f".{output.stem}.{token}.partial.mp4")
        manifest: Path | None = None

        if len(inputs) == 1:
            input_args = ["-ss", str(offset), "-i", str(inputs[0])]
        else:
            manifest = output.with_name(f".{output.stem}.{token}.txt")
            manifest.write_text(
                "".join(
                    f"file '{str(path).replace(chr(39), chr(39) * 2)}'\n"
                    for path in inputs
                ),
                encoding="utf-8",
            )
            input_args = ["-ss", str(offset), "-f", "concat", "-safe", "0", "-i", str(manifest)]

        codecs = {
            str(item.codec or source.detected_codec or source.expected_codec or "").lower()
            for item in segments
        }
        video_args = (
            ["-c:v", "copy"]
            if codecs <= {"h264", "avc1", ""}
            else ["-c:v", "libx264", "-preset", "veryfast"]
        )
        command = [
            executable,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            *input_args,
            "-t",
            str(duration),
            "-map",
            "0:v:0",
            "-an",
            *video_args,
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(temporary),
        ]
        try:
            result = subprocess.run(command, capture_output=True, timeout=180, check=False)
            if result.returncode or not temporary.is_file() or temporary.stat().st_size <= 0:
                raise PlaybackArtifactError("Event video could not be prepared")
            os.replace(temporary, output)
        except (OSError, subprocess.SubprocessError) as error:
            raise PlaybackArtifactError("Event video could not be prepared") from error
        finally:
            temporary.unlink(missing_ok=True)
            if manifest:
                manifest.unlink(missing_ok=True)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def cleanup_expired_playback_artifacts(
    db: Session, *, now: datetime | None = None
) -> int:
    """Remove finalized playback cache files after their configured lifetime."""
    cutoff = _utc(now or datetime.now(timezone.utc)) - timedelta(
        minutes=settings.media_playback_cache_minutes
    )
    removed = 0
    for storage in db.scalars(select(MediaStorage)).all():
        cache_dir = Path(storage.root_path).resolve() / ".playback-cache"
        if not cache_dir.is_dir():
            continue
        for item in cache_dir.glob("event_*.mp4"):
            try:
                modified = datetime.fromtimestamp(item.stat().st_mtime, timezone.utc)
                if modified < cutoff:
                    item.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
    return removed
