from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.camera import Camera
from app.models.media import EventMediaReference, MediaEvidenceHold, MediaExport, MediaSource, MediaStorage, RecordingSegment
from app.models.recognition_event import RecognitionEvent
from app.models.site import Site
from app.services.media_provider import CentralRecorderProvider
from app.services.media_service import EventMediaService, MediaConfigurationService, MediaPlaybackService, MediaValidationError, validate_stream_uri
from app.services.recorder_service import _recording_segment_start_state, apply_retention, process_exports


UTC = timezone.utc


def camera(db):
    site = Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur")
    item = Camera(site=site, code="MAIN-ENTRY-01", name="Main Entry", direction="ENTRY", is_enabled=True)
    db.add(item); db.commit(); return item


def configured_source(db, tmp_path: Path):
    item = camera(db)
    storage = MediaStorage(name="Primary", storage_type="LOCAL", root_path=str(tmp_path), enabled=True,
        retention_days=30, minimum_free_space_gb=0, minimum_free_space_percent=0)
    db.add(storage); db.flush()
    source = MediaSource(camera_id=item.id, provider_type="CENTRAL_RECORDER", enabled=True,
        recording_enabled=True, recording_stream_uri="rtsp://camera.local/main", storage_id=storage.id,
        pre_event_seconds=10, post_event_seconds=15, segment_duration_seconds=60, recorder_state="RECORDING")
    db.add(source); db.commit(); return item, storage, source


def event(db, item, at):
    row = RecognitionEvent(camera_id=item.id, site_id=item.site_id, plate_text="VAB1234", direction="ENTRY",
        captured_at=at, received_at=at, event_type="ENRICHED", source="LIVE")
    db.add(row); db.commit(); return row


def segment(db, source, start, end, relative="segment.mp4", state="COMPLETE"):
    row = RecordingSegment(media_source_id=source.id, camera_id=source.camera_id, started_at=start,
        ended_at=end, relative_path=relative, state=state, codec="h264")
    db.add(row); db.commit(); return row


def test_credentials_are_rejected_from_rtsp_uri():
    with pytest.raises(MediaValidationError, match="credential reference"):
        validate_stream_uri("rtsp://admin:secret@camera.local/main")
    assert validate_stream_uri("rtsp://camera.local/main") == "rtsp://camera.local/main"


def test_media_configuration_validates_event_window_and_storage(db, tmp_path):
    item = camera(db)
    with pytest.raises(MediaValidationError, match="enabled storage"):
        MediaConfigurationService(db).save_source(item, provider_type="CENTRAL_RECORDER", enabled=True,
            recording_enabled=True, recording_stream_uri="rtsp://camera.local/main", live_stream_uri=None,
            credential_reference=None, storage_id=None, pre_event_seconds=10, post_event_seconds=15,
            segment_duration_seconds=60, media_time_offset_ms=0, expected_codec="h264")
    with pytest.raises(MediaValidationError, match="safe range"):
        MediaConfigurationService(db).save_source(item, provider_type="NONE", enabled=False,
            recording_enabled=False, recording_stream_uri=None, live_stream_uri=None, credential_reference=None,
            storage_id=None, pre_event_seconds=61, post_event_seconds=15, segment_duration_seconds=60,
            media_time_offset_ms=0, expected_codec=None)


def test_segment_coverage_full_partial_gap_and_boundaries(db, tmp_path):
    item, _, source = configured_source(db, tmp_path); base = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
    segment(db, source, base, base + timedelta(seconds=60), "one.mp4")
    segment(db, source, base + timedelta(seconds=60), base + timedelta(seconds=120), "two.mp4")
    provider = CentralRecorderProvider(db)
    assert provider.get_recording_availability(item.id, base, base + timedelta(seconds=120)).status == "AVAILABLE"
    assert provider.get_recording_availability(item.id, base - timedelta(seconds=10), base + timedelta(seconds=10)).status == "PARTIAL"
    assert provider.get_recording_availability(item.id, base + timedelta(seconds=120), base + timedelta(seconds=130)).status == "UNAVAILABLE"


def test_event_reference_window_and_offset_do_not_change_recognition_time(db, tmp_path):
    item, _, source = configured_source(db, tmp_path); source.media_time_offset_ms = 1200; db.commit()
    captured = datetime.now(UTC) - timedelta(minutes=1); row = event(db, item, captured)
    ref = EventMediaService(db).ensure_reference(row.id)
    assert ref.status == "PENDING"
    assert ref.media_anchor_at.replace(tzinfo=UTC) == captured + timedelta(milliseconds=1200)
    assert ref.clip_start_at.replace(tzinfo=UTC) == captured - timedelta(seconds=8, milliseconds=800)
    assert ref.clip_end_at.replace(tzinfo=UTC) == captured + timedelta(seconds=16, milliseconds=200)
    assert row.captured_at.replace(tzinfo=UTC) == captured


def test_disabled_source_is_not_configured(db, tmp_path):
    item, _, source = configured_source(db, tmp_path); source.recording_enabled = False; source.enabled = False; db.commit()
    row = event(db, item, datetime.now(UTC) - timedelta(minutes=1)); ref = EventMediaService(db).ensure_reference(row.id)
    assert ref.status == "NOT_CONFIGURED"


def test_historical_offline_event_can_resolve_existing_footage(db, tmp_path):
    item, _, source = configured_source(db, tmp_path); captured = datetime.now(UTC) - timedelta(hours=2)
    row = event(db, item, captured); row.source = "OFFLINE_RECOVERY"; db.commit()
    segment(db, source, captured - timedelta(seconds=20), captured + timedelta(seconds=30))
    ref = EventMediaService(db).for_event(row.id)
    assert ref.status == "AVAILABLE"


def test_playback_range_is_bounded(db, tmp_path):
    item, _, _ = configured_source(db, tmp_path); start = datetime.now(UTC)
    with pytest.raises(MediaValidationError, match="limited"):
        MediaPlaybackService(db).historical(item.id, start, start + timedelta(hours=2))


def test_live_source_uses_enabled_central_recorder(db, tmp_path):
    item, _, source = configured_source(db, tmp_path)
    assert MediaPlaybackService(db).live_source(item.id).id == source.id
    source.enabled = False; db.commit()
    with pytest.raises(MediaValidationError, match="not configured"):
        MediaPlaybackService(db).live_source(item.id)


def test_recorder_status_stays_recording_between_healthy_segments(db, tmp_path):
    _, _, source = configured_source(db, tmp_path)
    assert _recording_segment_start_state(source) == "STARTING"

    source.last_successful_segment_at = datetime.now(UTC)
    source.recorder_state = "STARTING"
    assert _recording_segment_start_state(source) == "RECORDING"

    source.recorder_state = "DISCONNECTED"
    assert _recording_segment_start_state(source) == "DEGRADED"


def test_segment_path_rejects_traversal(db, tmp_path):
    item, storage, source = configured_source(db, tmp_path)
    row = RecordingSegment(media_source_id=source.id, camera_id=item.id, started_at=datetime.now(UTC),
        relative_path="../secret.mp4", state="COMPLETE")
    with pytest.raises(MediaValidationError, match="Invalid media path"):
        MediaPlaybackService.segment_path(row, storage)


def test_retention_preserves_held_evidence_and_removes_unprotected(db, tmp_path):
    item, storage, source = configured_source(db, tmp_path); storage.retention_days = 1
    old = datetime.now(UTC) - timedelta(days=10)
    kept = segment(db, source, old, old + timedelta(minutes=1), "kept.mp4")
    removed = segment(db, source, old + timedelta(minutes=2), old + timedelta(minutes=3), "removed.mp4")
    (tmp_path / kept.relative_path).write_bytes(b"held"); (tmp_path / removed.relative_path).write_bytes(b"remove")
    db.add(MediaEvidenceHold(camera_id=item.id, start_at=old, end_at=old + timedelta(minutes=1), reason="Incident", created_by_user_id=None)); db.commit()
    assert apply_retention(db) == 1
    assert db.get(RecordingSegment, kept.id) is not None
    assert db.get(RecordingSegment, removed.id) is None
    assert (tmp_path / "kept.mp4").exists() and not (tmp_path / "removed.mp4").exists()


def test_export_is_queued_then_completed_with_sha256(db, tmp_path, monkeypatch):
    item, _, source = configured_source(db, tmp_path); start = datetime.now(UTC) - timedelta(minutes=2)
    row = segment(db, source, start, start + timedelta(minutes=1)); (tmp_path / row.relative_path).write_bytes(b"source-video")
    job = MediaExport(requested_by_user_id=None, camera_id=item.id, start_at=start + timedelta(seconds=5),
        end_at=start + timedelta(seconds=15), state="QUEUED")
    db.add(job); db.commit()
    def fake_run(command, **kwargs):
        Path(command[-1]).write_bytes(b"bounded-export")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("app.services.recorder_service.subprocess.run", fake_run)
    assert process_exports(db) == 1
    db.refresh(job)
    assert job.state == "COMPLETED" and len(job.checksum_sha256) == 64
    assert job.output_relative_path.startswith("exports/")
