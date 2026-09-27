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
from app.services.camera_service import CameraService, CameraValidationError
from app.services.dashboard_service import get_dashboard_summary
from app.services.mqtt_service import mqtt_heartbeat_service
from app.services.shell_service import build_navigation, role_label
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
            "can_manage_cameras": user_has_permission(user, "cameras.manage"),
        }
    )
    return templates.TemplateResponse(
        request=request,
        name="cameras/detail.html",
        context=context,
    )


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
