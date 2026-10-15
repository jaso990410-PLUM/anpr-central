from __future__ import annotations

import re
import json
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password
from app.api.media import _parse_utc
from app.db.dependencies import get_db
from app.main import app
from app.models.camera import Camera
from app.models.media import MediaSource, MediaStorage, RecordingSegment
from app.models.permission import Permission
from app.models.recognition_event import RecognitionEvent
from app.models.role import Role
from app.models.site import Site
from app.models.user import User


PASSWORD = "correct horse battery staple"


@pytest.fixture
def client(db: Session) -> Generator[TestClient, None, None]:
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    def override_db():
        with factory() as session:
            yield session
    app.dependency_overrides[get_db] = override_db
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def create_user(db: Session, permissions: tuple[str, ...]):
    role = Role(code="operator", name="Operator", is_system=True)
    role.permissions = [Permission(code=code, description=code) for code in permissions]
    user = User(username="operator", full_name="Media Operator", password_hash=hash_password(PASSWORD),
        is_active=True, must_change_password=False, failed_login_count=0, roles=[role])
    db.add(user); db.commit()


def seed(db: Session):
    site = Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur", is_active=True)
    camera = Camera(site=site, code="MAIN-ENTRY", name="Main Entry", direction="ENTRY", lane_role="ENTRY", is_enabled=True)
    db.add(camera); db.flush()
    at = datetime.now(timezone.utc)
    event = RecognitionEvent(camera_id=camera.id, site_id=site.id, plate_text="VAB1234", source="LIVE",
        confidence=98, captured_at=at, received_at=at, event_type="FULL")
    db.add(event); db.commit(); return camera, event


def csrf(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match; return match.group(1)


def login(client):
    page = client.get("/login")
    response = client.post("/login", data={"username":"operator","password":PASSWORD,"csrf_token":csrf(page.text),"next":"/media"})
    assert response.status_code == 303


def test_media_pages_require_authentication_and_permission(client, db):
    assert client.get("/media").status_code == 303
    assert client.get("/media/playback").status_code == 303
    create_user(db, ("cameras.view",)); login(client)
    assert client.get("/media").status_code == 403
    assert client.get("/media/playback").status_code == 403
    assert client.get("/media/storage/folders").status_code == 403


def test_media_overview_and_event_render_truthful_not_configured_state(client, db):
    create_user(db, ("media.view",)); _, event = seed(db); login(client)
    overview = client.get("/media")
    assert overview.status_code == 200 and "ANPR Recording" in overview.text
    page = client.get(f"/media/events/{event.id}")
    assert page.status_code == 200
    assert "NOT CONFIGURED" in page.text and "Continuous recording was not configured" in page.text
    assert "rtsp://" not in page.text


def test_media_time_parser_recovers_legacy_unescaped_positive_offset():
    parsed = _parse_utc("2026-10-13T21:37:42.379026 08:00")

    assert parsed == datetime(2026, 10, 13, 13, 37, 42, 379026, tzinfo=timezone.utc)


def test_recording_page_normalizes_legacy_offset_query_to_utc(client, db):
    create_user(db, ("media.view",)); camera, _ = seed(db); login(client)

    response = client.get(
        f"/media/cameras/{camera.id}",
        params={
            "start": "2026-10-13T21:37:42.379026 08:00",
            "end": "2026-10-13T21:38:42.379026 08:00",
        },
    )

    assert response.status_code == 302
    assert response.headers["location"].startswith(f"/media/playback?camera={camera.id}")
    assert "start=2026-10-13T21%3A37%3A42.379026%2008%3A00" in response.headers["location"]


def test_media_time_parser_treats_naive_operator_input_as_site_time():
    parsed = _parse_utc("2026-10-14T23:09:00", naive_timezone="Asia/Kuala_Lumpur")

    assert parsed == datetime(2026, 10, 14, 15, 9, tzinfo=timezone.utc)


def test_dashboard_live_view_uses_enabled_sources_without_exposing_rtsp(client, db):
    create_user(db, ("media.view",)); camera, _ = seed(db)
    db.add(MediaSource(camera_id=camera.id, provider_type="CENTRAL_RECORDER", enabled=True,
        recording_stream_uri="rtsp://camera.local/main", recorder_state="RECORDING"))
    db.commit(); login(client)

    dashboard = client.get("/dashboard")

    assert dashboard.status_code == 200
    assert "Live Camera View" in dashboard.text
    assert camera.code in dashboard.text
    assert f'/media/cameras/{camera.id}/live-stream' in dashboard.text
    assert "dashboard-live-view.js" in dashboard.text
    assert "rtsp://camera.local/main" not in dashboard.text
    assert 'data-dashboard-live-video' in dashboard.text
    assert 'data-dashboard-live-video src=' not in dashboard.text


def test_dashboard_hides_live_view_without_media_permission(client, db):
    create_user(db, ("cameras.view",)); seed(db); login(client)

    dashboard = client.get("/dashboard")

    assert dashboard.status_code == 200
    assert "Live Camera View" not in dashboard.text
    assert "dashboard-live-view.js" not in dashboard.text


def test_event_video_page_is_operator_focused_and_stream_supports_ranges(
    client, db, tmp_path, monkeypatch
):
    create_user(db, ("media.view", "media.manage", "media.export"))
    camera, event = seed(db)
    event.captured_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    event.received_at = event.captured_at
    storage = MediaStorage(
        name="Primary",
        storage_type="LOCAL",
        root_path=str(tmp_path),
        enabled=True,
        minimum_free_space_gb=0,
        minimum_free_space_percent=0,
    )
    db.add(storage)
    db.flush()
    source = MediaSource(
        camera_id=camera.id,
        provider_type="CENTRAL_RECORDER",
        enabled=True,
        recording_enabled=True,
        recording_stream_uri="rtsp://camera.local/main",
        storage_id=storage.id,
        pre_event_seconds=10,
        post_event_seconds=15,
        recorder_state="RECORDING",
    )
    db.add(source)
    db.flush()
    captured = event.captured_at
    db.add(
        RecordingSegment(
            media_source_id=source.id,
            camera_id=camera.id,
            started_at=captured - timedelta(seconds=30),
            ended_at=captured + timedelta(seconds=30),
            relative_path="segment.mp4",
            state="COMPLETE",
            codec="h264",
        )
    )
    db.commit()
    artifact = tmp_path / "event.mp4"
    artifact.write_bytes(b"0123456789abcdef")
    monkeypatch.setattr(
        "app.api.media.EventPlaybackArtifactService.prepare",
        lambda _self, **_kwargs: artifact,
    )
    login(client)

    page = client.get(f"/media/events/{event.id}")
    stream = client.get(
        f"/media/events/{event.id}/stream", headers={"Range": "bytes=2-5"}
    )

    assert page.status_code == 200
    assert "Back to Lane Operations" in page.text
    assert camera.name in page.text and camera.code in page.text
    assert "10 sec before" in page.text and "15 sec after recognition" in page.text
    assert "Replay from Recognition" in page.text
    assert f'/media/playback?camera={camera.id}&amp;event={event.id}&amp;zoom=15m' in page.text
    assert "Playback Details" in page.text
    assert "Protect Recording" in page.text and "Export Clip" in page.text
    assert "Queue Evidence Export" not in page.text
    assert "Playback, protection, and export never trigger" not in page.text
    assert 'autoplay muted playsinline preload="auto"' in page.text
    assert stream.status_code == 206
    assert stream.content == b"2345"
    assert stream.headers["accept-ranges"] == "bytes"
    assert stream.headers["content-range"] == "bytes 2-5/16"


def test_playback_workspace_renders_calendar_gaps_markers_and_local_time(client, db):
    create_user(db, ("media.view",))
    camera, recognized = seed(db)
    recognized.captured_at = datetime(2026, 10, 14, 16, 25, tzinfo=timezone.utc)
    recognized.received_at = recognized.captured_at
    storage = MediaStorage(name="Primary", storage_type="LOCAL", root_path="C:/private/media", enabled=True)
    db.add(storage); db.flush()
    source = MediaSource(camera_id=camera.id, provider_type="CENTRAL_RECORDER", enabled=True,
        recording_enabled=True, recording_stream_uri="rtsp://camera.local/main", storage_id=storage.id,
        recorder_state="RECORDING", media_time_offset_ms=1500)
    db.add(source); db.flush()
    db.add_all((
        RecordingSegment(media_source_id=source.id, camera_id=camera.id,
            started_at=datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 10, 14, 16, 10, tzinfo=timezone.utc), relative_path="one.mp4", state="COMPLETE"),
        RecordingSegment(media_source_id=source.id, camera_id=camera.id,
            started_at=datetime(2026, 10, 14, 16, 20, tzinfo=timezone.utc),
            ended_at=datetime(2026, 10, 14, 16, 40, tzinfo=timezone.utc), relative_path="two.mp4", state="COMPLETE"),
    ))
    db.commit(); login(client)

    page = client.get("/media/playback", params={"camera":str(camera.id), "date":"2026-10-15", "event":str(recognized.id), "zoom":"15m"})

    assert page.status_code == 200
    assert "Playback Workspace" in page.text and "24-hour" not in page.text
    assert "October 2026" in page.text and 'aria-current="date"' in page.text
    assert "Recorded footage available" in page.text
    assert camera.name in page.text and camera.code in page.text
    assert "Timeline" in page.text and "15 minutes" in page.text
    assert "VAB1234" in page.text and "Open Event Clip" in page.text
    assert "Advanced Search" in page.text and "Start (Asia/Kuala_Lumpur)" in page.text
    assert "rtsp://" not in page.text and "C:/private/media" not in page.text
    payload_text = re.search(r'<script type="application/json" id="playback-workspace-data">(.*?)</script>', page.text, re.S).group(1)
    payload = json.loads(payload_text)
    assert payload["timezone"] == "Asia/Kuala_Lumpur"
    assert len(payload["cameras"][0]["coverage"]) == 2
    assert payload["markers"][0]["local_time"] == "00:25:00"
    assert payload["markers"][0]["at"] == "2026-10-14T16:25:01.500000Z"
    assert payload["playhead"] == "2026-10-14T16:24:51.500000Z"
    assert payload["eventPrerollSeconds"] == 10
    assert 'data-event-lead-in' in page.text and '<option value="10" selected>' in page.text
    assert "media-playback.js?v=20261015.4" in page.text

    configured = client.get("/media/playback", params={
        "camera":str(camera.id), "date":"2026-10-15", "event":str(recognized.id),
        "zoom":"15m", "lead_in":15,
    })
    configured_payload = json.loads(re.search(
        r'<script type="application/json" id="playback-workspace-data">(.*?)</script>', configured.text, re.S
    ).group(1))
    assert configured_payload["playhead"] == "2026-10-14T16:24:46.500000Z"
    assert configured_payload["eventPrerollSeconds"] == 15


def test_historical_stream_reencodes_for_frame_accurate_event_preroll(client, db, tmp_path, monkeypatch):
    create_user(db, ("media.view",))
    camera, _ = seed(db)
    storage = MediaStorage(name="Primary", storage_type="LOCAL", root_path=str(tmp_path), enabled=True)
    db.add(storage); db.flush()
    source = MediaSource(camera_id=camera.id, provider_type="CENTRAL_RECORDER", enabled=True,
        recording_enabled=True, recording_stream_uri="rtsp://camera.local/main", storage_id=storage.id,
        recorder_state="RECORDING", detected_codec="h264")
    db.add(source); db.flush()
    segment_path = tmp_path / "segment.mp4"
    segment_path.write_bytes(b"recording")
    db.add(RecordingSegment(media_source_id=source.id, camera_id=camera.id,
        started_at=datetime(2026, 10, 14, 16, 20, tzinfo=timezone.utc),
        ended_at=datetime(2026, 10, 14, 16, 30, tzinfo=timezone.utc),
        relative_path=segment_path.name, state="COMPLETE", codec="h264"))
    db.commit(); login(client)
    commands = []

    class FakeProcess:
        def __init__(self): self.stdout = BytesIO(b"video")
        def poll(self): return 0
        def wait(self, timeout=None): return 0
        def terminate(self): return None
        def kill(self): return None

    monkeypatch.setattr("app.api.media.shutil.which", lambda _name: "ffmpeg")
    monkeypatch.setattr("app.api.media.subprocess.Popen", lambda command, **_kwargs: commands.append(command) or FakeProcess())

    response = client.get(f"/media/cameras/{camera.id}/playback", params={
        "start":"2026-10-14T16:24:58Z", "end":"2026-10-14T16:25:20Z",
    })

    assert response.status_code == 200 and response.content == b"video"
    assert "libx264" in commands[0]
    assert "copy" not in commands[0]
    assert "zerolatency" in commands[0]


def test_playback_workspace_supports_multiple_cameras_and_truthful_empty_coverage(client, db):
    create_user(db, ("media.view",)); first, _ = seed(db)
    second = Camera(site_id=first.site_id, code="MAIN-EXIT", name="Main Exit", direction="EXIT", lane_role="EXIT", is_enabled=True)
    db.add(second); db.flush()
    storage = MediaStorage(name="Primary", storage_type="LOCAL", root_path="C:/private/media", enabled=True)
    db.add(storage); db.flush()
    db.add_all((
        MediaSource(camera_id=first.id, provider_type="CENTRAL_RECORDER", enabled=True, recording_enabled=True,
            recording_stream_uri="rtsp://camera.local/entry", storage_id=storage.id, recorder_state="RECORDING"),
        MediaSource(camera_id=second.id, provider_type="CENTRAL_RECORDER", enabled=True, recording_enabled=True,
            recording_stream_uri="rtsp://camera.local/exit", storage_id=storage.id, recorder_state="RECORDING"),
    ))
    db.commit(); login(client)

    page = client.get("/media/playback", params=[("camera",str(first.id)),("camera",str(second.id)),("date","2026-10-15")])

    assert page.status_code == 200
    assert 'class="playback-video-grid layout-2"' in page.text
    assert page.text.count("data-playback-video") == 2
    payload_text = re.search(r'<script type="application/json" id="playback-workspace-data">(.*?)</script>', page.text, re.S).group(1)
    payload = json.loads(payload_text)
    assert len(payload["cameras"]) == 2
    assert all(camera["coverage"] == [] for camera in payload["cameras"])
    assert "has-recording" not in page.text


def test_playback_workspace_keeps_advanced_bounded_range_as_secondary_workflow(client, db):
    create_user(db, ("media.view",)); camera, _ = seed(db)
    storage = MediaStorage(name="Primary", storage_type="LOCAL", root_path="C:/private/media", enabled=True)
    db.add(storage); db.flush()
    source = MediaSource(camera_id=camera.id, provider_type="CENTRAL_RECORDER", enabled=True,
        recording_enabled=True, recording_stream_uri="rtsp://camera.local/main", storage_id=storage.id,
        recorder_state="RECORDING")
    db.add(source); db.commit(); login(client)

    page = client.get("/media/playback", params={"camera":str(camera.id), "start":"2026-10-15T00:10:00", "end":"2026-10-15T00:20:00"})

    assert page.status_code == 200
    assert '<details class="ops-panel playback-advanced" open>' in page.text
    assert 'value="2026-10-15T00:10:00"' in page.text
    assert 'value="2026-10-15T00:20:00"' in page.text


def test_camera_settings_present_operator_workflow_without_null_values(client, db):
    create_user(db, ("media.view", "media.manage", "cameras.view"))
    camera, _ = seed(db)
    db.add(
        MediaSource(
            camera_id=camera.id,
            provider_type="CENTRAL_RECORDER",
            enabled=True,
            recording_enabled=False,
            recording_stream_uri="rtsp://camera.local/main",
            live_stream_uri=None,
            credential_reference=None,
            pre_event_seconds=10,
            post_event_seconds=15,
            recorder_state="DISABLED",
        )
    )
    db.commit()
    login(client)

    page = client.get(f"/cameras/{camera.id}")

    assert page.status_code == 200
    assert "Enable 24/7 recording" in page.text
    assert "ANPR Event Clip" in page.text and "25 seconds total" in page.text
    assert "No authentication required" in page.text
    assert "Auto-detect (recommended)" in page.text
    assert "Advanced Stream Settings" in page.text
    assert "Technical Details &amp; MQTT Topics" in page.text
    assert "View Command Diagnostics" in page.text
    assert "Legacy direction label" not in page.text
    assert '<option value="EXTERNAL_NVR"' not in page.text
    assert 'value="None"' not in page.text
    assert ">None<" not in page.text


def test_storage_and_source_configuration_enforce_csrf_and_hide_credentials(client, db, tmp_path):
    create_user(db, ("media.view","media.manage","cameras.view")); camera, _ = seed(db); login(client)
    page = client.get("/media"); token = csrf(page.text)
    assert 'id="storage-root"' in page.text and "readonly" in page.text
    assert "Browse Folders" in page.text and "media-storage-picker.js" in page.text
    assert client.post("/media/storage", data={"name":"Primary","root_path":str(tmp_path),"storage_type":"LOCAL","enabled":"true","retention_days":"30","minimum_free_space_gb":"0","minimum_free_space_percent":"0"}).status_code == 403
    created = client.post("/media/storage", data={"csrf_token":token,"name":"Primary","root_path":str(tmp_path),"storage_type":"LOCAL","enabled":"true","retention_days":"30","minimum_free_space_gb":"0","minimum_free_space_percent":"0"})
    assert created.status_code == 303
    page = client.get(f"/cameras/{camera.id}"); token = csrf(page.text)
    bad = client.post(f"/media/cameras/{camera.id}/configure", data={"csrf_token":token,"provider_type":"CENTRAL_RECORDER","recording_stream_uri":"rtsp://admin:secret@camera.local/main","credential_reference":"CAMERA_ONE","pre_event_seconds":"10","post_event_seconds":"15","segment_duration_seconds":"60","media_time_offset_ms":"0","enabled":"true","recording_enabled":"true"})
    assert bad.status_code == 400 and "admin:secret" not in bad.text


def test_server_folder_picker_requires_manage_permission_and_lists_directories_only(client, db, tmp_path):
    create_user(db, ("media.view","media.manage")); login(client)
    child = tmp_path / "Recording"; child.mkdir(); (tmp_path / "secret.txt").write_text("not listed", encoding="utf-8")
    response = client.get("/media/storage/folders", params={"path":str(tmp_path)})
    assert response.status_code == 200
    payload = response.json()
    assert payload["current"] == str(tmp_path.resolve())
    assert payload["directories"] == [{"name":"Recording","path":str(child.resolve())}]
    page = client.get("/media"); token = csrf(page.text)
    assert client.post("/media/storage/folders", data={"parent_path":str(tmp_path),"folder_name":"Created Folder"}).status_code == 403
    created = client.post("/media/storage/folders", data={"csrf_token":token,"parent_path":str(tmp_path),"folder_name":"Created Folder"})
    assert created.status_code == 200 and (tmp_path / "Created Folder").is_dir()
    traversal = client.post("/media/storage/folders", data={"csrf_token":token,"parent_path":str(tmp_path),"folder_name":"../escape"})
    assert traversal.status_code == 400 and not (tmp_path.parent / "escape").exists()
    duplicate = client.post("/media/storage/folders", data={"csrf_token":token,"parent_path":str(tmp_path),"folder_name":"Created Folder"})
    assert duplicate.status_code == 409


def test_export_permission_is_separate(client, db):
    create_user(db, ("media.view",)); _, event = seed(db); login(client)
    page = client.get(f"/media/events/{event.id}")
    assert "Queue Evidence Export" not in page.text
    assert client.post(f"/media/events/{event.id}/export", data={"csrf_token":csrf(page.text)}).status_code == 403
