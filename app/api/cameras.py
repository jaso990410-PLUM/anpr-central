from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.db.dependencies import get_db
from app.dependencies.auth import require_page_permission, user_has_permission
from app.dependencies.csrf import require_session_csrf
from app.models.camera import Camera
from app.models.user import User
from app.services.audit_service import create_audit_log
from app.services.access_list_service import AccessListService
from app.models.access_list_entry import AccessListEntry
from sqlalchemy import select
from app.services.camera_service import CameraService, CameraValidationError
from app.services.dashboard_service import get_dashboard_summary
from app.services.mqtt_service import mqtt_heartbeat_service
from app.services.offline_recovery_service import OfflineRecoveryRejected
from app.services.shell_service import build_navigation, role_label
from app.services.media_service import MediaConfigurationService, safe_stream_label, storage_health
from app.web import templates


router = APIRouter(prefix="/cameras")


def _shell_context(
    request: Request,
    db: Session,
    user: User,
    raw_session_token: str,
) -> dict[str, object]:
    return {
        "request": request,
        "user": user,
        "csrf_token": generate_csrf_token(raw_session_token),
        "summary": get_dashboard_summary(db),
        "active_page": "cameras",
        "navigation": build_navigation(user),
        "user_role_label": role_label(user),
        "mqtt_connected": mqtt_heartbeat_service.is_connected,
        "live_poll_seconds": settings.live_poll_seconds,
    }


def _camera_or_404(service: CameraService, camera_id: UUID) -> Camera:
    camera = service.get_camera(camera_id)
    if camera is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Camera not found",
        )
    return camera


def _render_create_form(
    request: Request,
    db: Session,
    user: User,
    raw_session_token: str,
    *,
    values: dict[str, object] | None = None,
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    context = _shell_context(request, db, user, raw_session_token)
    context.update(
        {
            "sites": CameraService(db).list_sites(),
            "values": values or {"direction": "ENTRY", "is_enabled": True},
            "error": error,
        }
    )
    return templates.TemplateResponse(
        request=request,
        name="cameras/new.html",
        context=context,
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def camera_list(
    request: Request,
    user: User = Depends(require_page_permission("cameras.view")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    raw_session_token = request.cookies[settings.session_cookie_name]
    context = _shell_context(request, db, user, raw_session_token)
    context.update(
        {
            "cameras": CameraService(db).list_cameras(),
            "can_manage_cameras": user_has_permission(user, "cameras.manage"),
            "can_view_control_board": user_has_permission(user, "control_board.view"),
            "can_view_barrier": user_has_permission(user, "cameras.view"),
        }
    )
    return templates.TemplateResponse(
        request=request,
        name="cameras/list.html",
        context=context,
    )


@router.get("/new", response_class=HTMLResponse)
def new_camera_page(
    request: Request,
    user: User = Depends(require_page_permission("cameras.manage")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    raw_session_token = request.cookies[settings.session_cookie_name]
    return _render_create_form(request, db, user, raw_session_token)


@router.post("/new", response_class=HTMLResponse)
def create_camera(
    request: Request,
    site_id: Annotated[str, Form()],
    code: Annotated[str, Form()],
    name: Annotated[str, Form()],
    direction: Annotated[str, Form()],
    manufacturer: Annotated[str | None, Form()] = None,
    model: Annotated[str | None, Form()] = None,
    serial_number: Annotated[str | None, Form()] = None,
    ip_address: Annotated[str | None, Form()] = None,
    is_enabled: Annotated[str | None, Form()] = None,
    user: User = Depends(require_page_permission("cameras.manage")),
    raw_session_token: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> Response:
    enabled = is_enabled == "true"
    safe_values: dict[str, object] = {
        "site_id": site_id,
        "code": code,
        "name": name,
        "direction": direction,
        "manufacturer": manufacturer or "",
        "model": model or "",
        "serial_number": serial_number or "",
        "ip_address": ip_address or "",
        "is_enabled": enabled,
    }
    service = CameraService(db)
    try:
        camera = service.create_camera(
            site_id=site_id,
            code=code,
            name=name,
            direction=direction,
            manufacturer=manufacturer,
            model=model,
            serial_number=serial_number,
            ip_address=ip_address,
            is_enabled=enabled,
        )
    except CameraValidationError as error:
        return _render_create_form(
            request,
            db,
            user,
            raw_session_token,
            values=safe_values,
            error=str(error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    create_audit_log(
        db,
        action="camera.create",
        actor_user_id=user.id,
        site_id=camera.site_id,
        entity_type="camera",
        entity_id=str(camera.id),
        details={
            "camera_id": str(camera.id),
            "code": camera.code,
            "site_id": str(camera.site_id),
            "direction": camera.direction,
            "provisioning_state": camera.provisioning_state,
            "is_enabled": camera.is_enabled,
        },
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return RedirectResponse(
        f"/cameras/{camera.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
@router.get("/{camera_id}", response_class=HTMLResponse)
def camera_detail(
    camera_id: UUID,
    request: Request,
    user: User = Depends(require_page_permission("cameras.view")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    service = CameraService(db)
    camera = _camera_or_404(service, camera_id)
    raw_session_token = request.cookies[settings.session_cookie_name]
    context = _shell_context(request, db, user, raw_session_token)
    context.update(
        {
            "camera": service.camera_detail(camera),
            "recent_commands": service.recent_commands(camera),
            "access_sync_summary": service.access_list_sync_summary(camera),
            "offline_recovery": service.offline_recovery_summary(camera),
            "can_manage_cameras": user_has_permission(user, "cameras.manage"),
            "can_view_control_board": user_has_permission(user, "control_board.view"),
            "can_view_barrier": user_has_permission(user, "cameras.view"),
            "can_delete_all_access_list": user_has_permission(user, "access_list.delete_all_camera"),
            "can_view_media": user_has_permission(user, "media.view"),
            "can_manage_media": user_has_permission(user, "media.manage"),
            "media_source": MediaConfigurationService(db).source_for_camera(camera.id),
            "media_storages": MediaConfigurationService(db).storages(),
            "safe_stream_label": safe_stream_label,
            "storage_health": storage_health,
        }
    )
    return templates.TemplateResponse(
        request=request,
        name="cameras/detail.html",
        context=context,
    )


@router.post("/{camera_id}/offline-recovery")
def recover_offline_recognitions(
    camera_id: UUID,
    request: Request,
    user: User = Depends(require_page_permission("cameras.manage")),
    _csrf: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    camera = _camera_or_404(CameraService(db), camera_id)
    try:
        run = mqtt_heartbeat_service.recover_offline_events(camera.id)
    except (RuntimeError, OfflineRecoveryRejected) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    create_audit_log(
        db,
        action="camera.offline_recovery_request",
        actor_user_id=user.id,
        site_id=camera.site_id,
        entity_type="camera",
        entity_id=str(camera.id),
        details={
            "camera_code": camera.code,
            "recovery_run_id": str(run.id),
            "reported_backlog": run.last_reported_count,
            "requested_max_count": run.requested_max_count,
        },
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return RedirectResponse(
        f"/cameras/{camera.id}?offline_recovery=requested",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/{camera_id}/lane-role")
def update_camera_lane_role(
    camera_id: UUID,
    request: Request,
    lane_role: Annotated[str, Form()],
    user: User = Depends(require_page_permission("cameras.manage")),
    _csrf: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    service = CameraService(db)
    camera = _camera_or_404(service, camera_id)
    try:
        changed = service.set_lane_role(camera, lane_role)
    except CameraValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if changed:
        create_audit_log(db, action="camera.lane_role_update", actor_user_id=user.id,
            site_id=camera.site_id, entity_type="camera", entity_id=str(camera.id),
            details={"camera_code": camera.code, "lane_role": camera.lane_role},
            ip_address=request.client.host if request.client else None)
    db.commit()
    return RedirectResponse(f"/cameras/{camera.id}", status_code=303)


@router.post("/{camera_id}/access-list-sync/{action}")
def set_camera_access_list_sync(
    camera_id: UUID,
    action: str,
    request: Request,
    user: User = Depends(require_page_permission("cameras.manage")),
    _raw_session_token: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if action not in {"enable", "disable"}:
        raise HTTPException(status_code=404, detail="Unknown camera action")
    service = CameraService(db)
    camera = _camera_or_404(service, camera_id)
    enabled = action == "enable"
    if service.set_access_list_sync(camera, enabled=enabled):
        if enabled:
            access_service = AccessListService(db)
            entries = db.scalars(select(AccessListEntry).where(AccessListEntry.site_id == camera.site_id)).all()
            for entry in entries:
                access_service.ensure_sync_records(entry)
        create_audit_log(
            db,
            action=f"camera.access_list_sync_{action}",
            actor_user_id=user.id,
            site_id=camera.site_id,
            entity_type="camera",
            entity_id=str(camera.id),
            details={"camera_code": camera.code, "enabled": enabled},
            ip_address=request.client.host if request.client else None,
        )
        db.commit()
    return RedirectResponse(f"/cameras/{camera.id}", status_code=303)


@router.post("/{camera_id}/access-list/delete-all")
def delete_all_camera_access_list(
    camera_id: UUID,
    request: Request,
    confirmation: Annotated[str, Form()],
    user: User = Depends(require_page_permission("access_list.delete_all_camera")),
    _csrf: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    camera = _camera_or_404(CameraService(db), camera_id)
    if confirmation != "DELETE ALL":
        raise HTTPException(status_code=400, detail="Type DELETE ALL exactly to confirm")
    try:
        command, affected = mqtt_heartbeat_service.delete_all_camera_access_records(camera.id)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    create_audit_log(db, action="access_list.camera_delete_all", actor_user_id=user.id, site_id=camera.site_id, entity_type="camera", entity_id=str(camera.id), details={"camera_code": camera.code, "command_id": str(command.id), "camera_sync_records_marked_out_of_sync": affected, "central_records_retained": True}, ip_address=request.client.host if request.client else None)
    db.commit()
    return RedirectResponse(f"/cameras/{camera.id}?access_list=cleared", status_code=303)


def _record_camera_state_action(
    request: Request,
    db: Session,
    user: User,
    camera: Camera,
    *,
    action: str,
) -> None:
    create_audit_log(
        db,
        action=action,
        actor_user_id=user.id,
        site_id=camera.site_id,
        entity_type="camera",
        entity_id=str(camera.id),
        details={
            "camera_id": str(camera.id),
            "code": camera.code,
            "provisioning_state": camera.provisioning_state,
            "is_enabled": camera.is_enabled,
        },
        ip_address=request.client.host if request.client else None,
    )


@router.post("/{camera_id}/enable")
def enable_camera(
    camera_id: UUID,
    request: Request,
    user: User = Depends(require_page_permission("cameras.manage")),
    _raw_session_token: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    service = CameraService(db)
    camera = _camera_or_404(service, camera_id)
    if service.set_enabled(camera, enabled=True):
        _record_camera_state_action(
            request, db, user, camera, action="camera.enable"
        )
    db.commit()
    return RedirectResponse(
        f"/cameras/{camera.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
@router.post("/{camera_id}/disable")
def disable_camera(
    camera_id: UUID,
    request: Request,
    user: User = Depends(require_page_permission("cameras.manage")),
    _raw_session_token: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    service = CameraService(db)
    camera = _camera_or_404(service, camera_id)
    if service.set_enabled(camera, enabled=False):
        _record_camera_state_action(
            request, db, user, camera, action="camera.disable"
        )
    db.commit()
    return RedirectResponse(
        f"/cameras/{camera.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
@router.post("/{camera_id}/provisioning/revoke")
def revoke_camera_provisioning(
    camera_id: UUID,
    request: Request,
    user: User = Depends(require_page_permission("cameras.manage")),
    _raw_session_token: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    service = CameraService(db)
    camera = _camera_or_404(service, camera_id)
    if service.revoke_provisioning(camera):
        _record_camera_state_action(
            request,
            db,
            user,
            camera,
            action="camera.provisioning_revoke",
        )
    db.commit()
    return RedirectResponse(
        f"/cameras/{camera.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/{camera_id}/provisioning/restore")
def restore_camera_provisioning(
    camera_id: UUID,
    request: Request,
    serial_number: Annotated[str, Form()],
    user: User = Depends(require_page_permission("cameras.manage")),
    _raw_session_token: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    service = CameraService(db)
    camera = _camera_or_404(service, camera_id)
    try:
        changed = service.restore_provisioning(
            camera, serial_number=serial_number
        )
    except CameraValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    if changed:
        _record_camera_state_action(
            request,
            db,
            user,
            camera,
            action="camera.provisioning_restore",
        )
    db.commit()
    return RedirectResponse(
        f"/cameras/{camera.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )

@router.post("/{camera_id}/commands/test-led")
def queue_camera_led_test(
    camera_id: UUID,
    request: Request,
    user: User = Depends(require_page_permission("cameras.manage")),
    _raw_session_token: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    service = CameraService(db)
    camera = _camera_or_404(service, camera_id)
    try:
        mqtt_heartbeat_service.enqueue_led_test(camera)
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    _record_camera_state_action(
        request,
        db,
        user,
        camera,
        action="camera.command_test_led",
    )
    db.commit()
    return RedirectResponse(
        f"/cameras/{camera.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
