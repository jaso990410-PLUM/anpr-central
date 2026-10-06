import re
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password
from app.db.dependencies import get_db
from app.main import app
from app.models.audit_log import AuditLog
from app.models.camera import Camera
from app.models.control_board import ControlBoard, PeripheralPreset
from app.models.permission import Permission
from app.models.role import Role
from app.models.site import Site
from app.models.user import User
from app.services.mqtt_service import mqtt_heartbeat_service


PASSWORD = "correct horse battery staple"


@pytest.fixture
def client(db: Session) -> Generator[TestClient, None, None]:
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    def override_db():
        with factory() as session:
            yield session
    app.dependency_overrides[get_db] = override_db
    with TestClient(app, follow_redirects=False) as value:
        yield value
    app.dependency_overrides.clear()


def seed(db: Session, permissions: tuple[str, ...]):
    role = Role(code="administrator", name="Administrator", is_system=True, permissions=[Permission(code=value, description=value) for value in permissions])
    user = User(username="admin", full_name="Administrator", password_hash=hash_password(PASSWORD), is_active=True, must_change_password=False, failed_login_count=0, roles=[role])
    site = Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur", is_active=True)
    camera = Camera(site=site, code="ENTRY-1", name="Main Entry", direction="ENTRY", serial_number="RA5-CB-WEB", is_enabled=True)
    db.add_all([user, camera]); db.commit()
    return camera


def token(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client: TestClient):
    page = client.get("/login")
    response = client.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": token(page.text), "next": "/dashboard"})
    assert response.status_code == 303


def test_permissions_sidebar_save_csrf_and_no_hardware_on_save(client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    camera = seed(db, ("cameras.view", "control_board.view", "control_board.manage"))
    login(client)
    page = client.get(f"/cameras/{camera.id}/control-board")
    assert page.status_code == 200 and "Read from Camera" not in page.text
    assert 'href="/peripheral-presets"' in page.text
    assert client.post(f"/cameras/{camera.id}/control-board", data={}).status_code == 403
    monkeypatch.setattr(mqtt_heartbeat_service, "apply_control_board", lambda *_args, **_kwargs: pytest.fail("save must not touch hardware"))
    response = client.post(f"/cameras/{camera.id}/control-board", data={"csrf_token": token(page.text), "enabled": "true", "serial_channel": "1", "baud_rate": "9600", "data_bits": "8", "parity": "NONE", "stop_bits": "1", "slave_id": "3", "default_preset_id": ""})
    assert response.status_code == 303
    board = db.scalar(select(ControlBoard))
    assert board is not None and board.serial_channel == 1 and board.slave_id == 3
    detail = client.get(f"/cameras/{camera.id}/control-board")
    assert "PENDING SYNC" in detail.text
    assert "Read from Camera" in detail.text and "Sync to Camera" in detail.text
    assert db.scalar(select(AuditLog).where(AuditLog.action == "control_board.create"))


def test_preset_create_edit_and_validation(client: TestClient, db: Session) -> None:
    seed(db, ("control_board.view", "control_board.manage"))
    login(client)
    page = client.get("/peripheral-presets/new")
    created = client.post("/peripheral-presets/new", data={"csrf_token": token(page.text), "name": "Normal", "description": "Daily", "enabled": "true"})
    assert created.status_code == 303
    detail = client.get(created.headers["location"])
    assert detail.status_code == 200 and "Normal Hours" in detail.text and "Peak Hours" in detail.text
    assert "data-busy-period-editor" in detail.text
    assert "data-busy-period-dialog" in detail.text
    assert 'data-dialog-day' in detail.text
    assert detail.text.count('type="time"') == 2
    assert "Add peak-hour period" in detail.text
    assert "Normal Hours" in detail.text and "Peak Hours" in detail.text
    assert "Peak-hour schedule" in detail.text
    assert "/static/js/busy-periods.js" in detail.text
    assert "data-speaker-enabled" in detail.text
    assert "data-speaker-setting hidden" in detail.text
    assert "data-row-source-field" in detail.text
    assert "data-row-text-field hidden" in detail.text
    editor_script = client.get("/static/js/busy-periods.js")
    assert "showModal()" in editor_script.text
    assert "Select a weekday, start time, and end time." in editor_script.text
    assert "overlaps another period" in editor_script.text
    preview_script = client.get("/static/js/peripheral-preview.js")
    assert "data-speaker-setting" in preview_script.text
    assert "data-row-source-field" in preview_script.text
    assert "data-row-text-field" in preview_script.text
    data = {"csrf_token": token(detail.text), "name": "Normal", "enabled": "true", "busy_periods": "0,08:00,17:00"}
    for mode in ("idle", "busy"):
        data.update({f"{mode}_traffic_state": "RED", f"{mode}_green_seconds": "5", f"{mode}_led_brightness": "4", f"{mode}_led_scroll_speed": "3", f"{mode}_speaker_volume": "7", f"{mode}_speaker_speed": "5", f"{mode}_speaker_pitch": "5", f"{mode}_speaker_voice": "3", f"{mode}_plate_pause_ms": "100", f"{mode}_speaker_message": "Welcome {plate}"})
        for row in range(1, 5): data.update({f"{mode}_row{row}_type": "STANDARD", f"{mode}_row{row}_source": "BLANK", f"{mode}_row{row}_text": ""})
    assert client.post(created.headers["location"], data=data).status_code == 303
    stored = db.scalar(select(PeripheralPreset))
    assert stored.busy_periods
    assert all(item.led_brightness == 4 and item.led_scroll_speed == 3 for item in stored.behaviors)
    rendered = client.get(created.headers["location"])
    assert 'data-day="0"' in rendered.text
    assert "Monday" in rendered.text and "08:00–17:00" in rendered.text


def test_control_board_test_requires_test_permission(client: TestClient, db: Session) -> None:
    camera = seed(db, ("control_board.view", "control_board.manage"))
    board = ControlBoard(camera=camera)
    db.add(board); db.commit()
    login(client)
    page = client.get(f"/cameras/{camera.id}/control-board")
    assert client.post(f"/cameras/{camera.id}/control-board/test/led", data={"csrf_token": token(page.text)}).status_code == 403


def test_rs485_read_and_sync_require_csrf_and_queue_explicit_commands(client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    camera = seed(db, ("control_board.view", "control_board.manage"))
    board = ControlBoard(camera=camera, slave_id=1)
    db.add(board); db.commit()
    login(client)
    page = client.get(f"/cameras/{camera.id}/control-board")
    assert client.post(f"/cameras/{camera.id}/control-board/communication/read").status_code == 403
    calls = []
    monkeypatch.setattr(mqtt_heartbeat_service, "read_control_board_rs485", lambda board_id: calls.append(("read", board_id)) or board.id)
    monkeypatch.setattr(mqtt_heartbeat_service, "sync_control_board_rs485", lambda board_id: calls.append(("sync", board_id)) or board.id)
    csrf_value = token(page.text)
    assert client.post(f"/cameras/{camera.id}/control-board/communication/read", data={"csrf_token": csrf_value}).status_code == 303
    assert client.post(f"/cameras/{camera.id}/control-board/communication/sync", data={"csrf_token": csrf_value}).status_code == 303
    assert calls == [("read", board.id), ("sync", board.id)]
