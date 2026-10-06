from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.db.dependencies import get_db
from app.dependencies.auth import require_page_permission, user_has_permission
from app.dependencies.csrf import require_session_csrf
from app.models.camera import Camera
from app.models.user import User
from app.services.audit_service import create_audit_log
from app.services.control_board_runtime_service import ControlBoardRuntimeService
from app.services.control_board_service import ControlBoardService, ControlBoardValidationError
from app.services.dashboard_service import get_dashboard_summary
from app.services.mqtt_service import mqtt_heartbeat_service
from app.services.shell_service import build_navigation, role_label
from app.web import templates


router = APIRouter(prefix="/cameras/{camera_id}/control-board")


def _camera(db: Session, camera_id: UUID) -> Camera:
    camera = db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    return camera


def _context(request: Request, db: Session, user: User, camera: Camera, *, error=None, notice=None):
    service = ControlBoardService(db)
    board = service.get_for_camera(camera.id)
    resolved = ControlBoardRuntimeService(db).resolve(board) if board else None
    return {
        "request": request, "user": user,
        "csrf_token": generate_csrf_token(request.cookies[settings.session_cookie_name]),
        "summary": get_dashboard_summary(db), "active_page": "cameras",
        "navigation": build_navigation(user), "user_role_label": role_label(user),
        "camera": camera, "board": board, "presets": service.presets(),
        "resolved": resolved, "error": error, "notice": notice,
        "parity_labels": {0: "None", 1: "Odd", 2: "Even"},
        "can_manage": user_has_permission(user, "control_board.manage"),
        "can_test": user_has_permission(user, "control_board.test"),
        "mqtt_connected": mqtt_heartbeat_service.is_connected,
    }


@router.get("", response_class=HTMLResponse)
def detail(request: Request, camera_id: UUID, user: User = Depends(require_page_permission("control_board.view")), db: Session = Depends(get_db)):
    return templates.TemplateResponse(request=request, name="control_boards/detail.html", context=_context(request, db, user, _camera(db, camera_id)))


@router.post("", response_class=HTMLResponse)
async def save(request: Request, camera_id: UUID, user: User = Depends(require_page_permission("control_board.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    camera = _camera(db, camera_id)
    form = dict(await request.form())
    service = ControlBoardService(db)
    try:
        board, created = service.save(camera, enabled=form.get("enabled") == "true", serial_channel=form.get("serial_channel"), baud_rate=form.get("baud_rate"), data_bits=form.get("data_bits"), parity=form.get("parity"), stop_bits=form.get("stop_bits"), slave_id=form.get("slave_id"), default_preset_id=form.get("default_preset_id"))
        create_audit_log(db, action="control_board.create" if created else "control_board.update", actor_user_id=user.id, site_id=camera.site_id, entity_type="control_board", entity_id=str(board.id), details={"camera_id": str(camera.id), "enabled": board.enabled, "serial_channel": board.serial_channel, "baud_rate": board.baud_rate, "data_bits": board.data_bits, "parity": board.parity, "stop_bits": board.stop_bits, "slave_id": board.slave_id, "default_preset_id": str(board.default_preset_id) if board.default_preset_id else None}, ip_address=request.client.host if request.client else None)
        db.commit()
    except ControlBoardValidationError as error:
        db.rollback()
        return templates.TemplateResponse(request=request, name="control_boards/detail.html", context=_context(request, db, user, camera, error=str(error)), status_code=status.HTTP_400_BAD_REQUEST)
    return RedirectResponse(f"/cameras/{camera_id}/control-board?saved=1", status_code=303)


@router.post("/communication/read")
def read_communication(request: Request, camera_id: UUID, user: User = Depends(require_page_permission("control_board.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    camera = _camera(db, camera_id)
    board = ControlBoardService(db).get_for_camera(camera.id)
    if board is None:
        raise HTTPException(status_code=404, detail="Control board not configured")
    try:
        command_id = mqtt_heartbeat_service.read_control_board_rs485(board.id)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    create_audit_log(db, action="control_board.communication_read", actor_user_id=user.id, site_id=camera.site_id, entity_type="control_board", entity_id=str(board.id), details={"camera_id": str(camera.id), "command_id": str(command_id), "capability": "documented_not_physically_verified"}, ip_address=request.client.host if request.client else None)
    db.commit()
    return RedirectResponse(f"/cameras/{camera_id}/control-board?communication=read", status_code=303)


@router.post("/communication/sync")
def sync_communication(request: Request, camera_id: UUID, user: User = Depends(require_page_permission("control_board.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    camera = _camera(db, camera_id)
    board = ControlBoardService(db).get_for_camera(camera.id)
    if board is None:
        raise HTTPException(status_code=404, detail="Control board not configured")
    try:
        command_id = mqtt_heartbeat_service.sync_control_board_rs485(board.id)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    create_audit_log(db, action="control_board.communication_sync", actor_user_id=user.id, site_id=camera.site_id, entity_type="control_board", entity_id=str(board.id), details={"camera_id": str(camera.id), "command_id": str(command_id), "source": board.serial_channel, "baud_rate": board.baud_rate, "data_bits": board.data_bits, "parity": board.parity, "stop_bits": board.stop_bits, "capability": "documented_not_physically_verified"}, ip_address=request.client.host if request.client else None)
    db.commit()
    return RedirectResponse(f"/cameras/{camera_id}/control-board?communication=sync", status_code=303)


@router.post("/test/{action}")
def test_action(request: Request, camera_id: UUID, action: str, user: User = Depends(require_page_permission("control_board.test")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    camera = _camera(db, camera_id)
    board = ControlBoardService(db).get_for_camera(camera.id)
    if board is None:
        raise HTTPException(status_code=404, detail="Control board not configured")
    try:
        count = mqtt_heartbeat_service.test_control_board(board.id, action)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    create_audit_log(db, action="control_board.test", actor_user_id=user.id, site_id=camera.site_id, entity_type="control_board", entity_id=str(board.id), details={"action": action, "commands_queued": count, "verification": "camera_acknowledgement_only"}, ip_address=request.client.host if request.client else None)
    db.commit()
    return RedirectResponse(f"/cameras/{camera_id}/control-board?test=queued", status_code=303)


@router.post("/apply")
def apply_runtime(request: Request, camera_id: UUID, user: User = Depends(require_page_permission("control_board.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    camera = _camera(db, camera_id)
    board = ControlBoardService(db).get_for_camera(camera.id)
    if board is None:
        raise HTTPException(status_code=404, detail="Control board not configured")
    try:
        mqtt_heartbeat_service.apply_control_board(camera.id)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    create_audit_log(db, action="control_board.runtime_apply", actor_user_id=user.id, site_id=camera.site_id, entity_type="control_board", entity_id=str(board.id), details={"camera_id": str(camera.id)}, ip_address=request.client.host if request.client else None)
    db.commit()
    return RedirectResponse(f"/cameras/{camera_id}/control-board?apply=queued", status_code=303)


@router.post("/schedules")
async def add_schedule(request: Request, camera_id: UUID, user: User = Depends(require_page_permission("control_board.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    camera = _camera(db, camera_id)
    service = ControlBoardService(db)
    board = service.get_for_camera(camera.id)
    if board is None:
        raise HTTPException(status_code=404, detail="Control board not configured")
    form = dict(await request.form())
    try:
        schedule = service.add_schedule(board, preset_id=form.get("preset_id"), starts_at=form.get("starts_at"), ends_at=form.get("ends_at"), enabled=True)
        create_audit_log(db, action="control_board.schedule_create", actor_user_id=user.id, site_id=camera.site_id, entity_type="peripheral_preset_schedule", entity_id=str(schedule.id), details={"control_board_id": str(board.id), "preset_id": str(schedule.preset_id), "starts_at": schedule.starts_at.isoformat(), "ends_at": schedule.ends_at.isoformat()}, ip_address=request.client.host if request.client else None)
        db.commit()
    except ControlBoardValidationError as error:
        db.rollback()
        return templates.TemplateResponse(request=request, name="control_boards/detail.html", context=_context(request, db, user, camera, error=str(error)), status_code=400)
    return RedirectResponse(f"/cameras/{camera_id}/control-board", status_code=303)


@router.post("/schedules/{schedule_id}/delete")
def delete_schedule(request: Request, camera_id: UUID, schedule_id: UUID, user: User = Depends(require_page_permission("control_board.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    camera = _camera(db, camera_id)
    service = ControlBoardService(db)
    board = service.get_for_camera(camera.id)
    if board is None or not service.delete_schedule(board, schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    create_audit_log(db, action="control_board.schedule_delete", actor_user_id=user.id, site_id=camera.site_id, entity_type="peripheral_preset_schedule", entity_id=str(schedule_id), details={"control_board_id": str(board.id)}, ip_address=request.client.host if request.client else None)
    db.commit()
    return RedirectResponse(f"/cameras/{camera_id}/control-board", status_code=303)
