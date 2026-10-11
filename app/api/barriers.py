from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.db.dependencies import get_db
from app.dependencies.auth import require_page_permission, user_has_permission
from app.dependencies.csrf import require_session_csrf
from app.models.barrier import BarrierAction, BarrierConfiguration, BarrierState
from app.models.camera import Camera
from app.models.parking import ParkingReviewItem
from app.models.user import User
from app.services.audit_service import create_audit_log
from app.services.barrier_action_service import BarrierActionError
from app.services.barrier_configuration_service import BarrierConfigurationError, BarrierConfigurationService
from app.services.dashboard_service import get_dashboard_summary
from app.services.mqtt_service import mqtt_heartbeat_service
from app.services.shell_service import build_navigation, role_label
from app.web import templates


router = APIRouter()


def _context(request: Request, db: Session, user: User, active_page: str) -> dict[str, object]:
    raw = request.cookies[settings.session_cookie_name]
    return {
        "request": request,
        "user": user,
        "csrf_token": generate_csrf_token(raw),
        "summary": get_dashboard_summary(db),
        "active_page": active_page,
        "navigation": build_navigation(user),
        "user_role_label": role_label(user),
        "mqtt_connected": mqtt_heartbeat_service.is_connected,
    }


def _camera(db: Session, camera_id: UUID) -> Camera:
    camera = db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(404, "Camera not found")
    return camera


def _audit(request: Request, db: Session, user: User, *, action: str, camera: Camera, details: dict | None = None) -> None:
    create_audit_log(
        db,
        action=action,
        actor_user_id=user.id,
        site_id=camera.site_id,
        entity_type="camera",
        entity_id=str(camera.id),
        details=details,
        ip_address=request.client.host if request.client else None,
    )


@router.get("/cameras/{camera_id}/barrier", response_class=HTMLResponse)
def barrier_page(
    camera_id: UUID,
    request: Request,
    user: User = Depends(require_page_permission("cameras.view")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    camera = _camera(db, camera_id)
    config = BarrierConfigurationService(db).get(camera.id)
    state = db.scalar(select(BarrierState).where(BarrierState.camera_id == camera.id))
    actions = tuple(db.scalars(
        select(BarrierAction)
        .options(joinedload(BarrierAction.requested_by))
        .where(BarrierAction.camera_id == camera.id)
        .order_by(BarrierAction.requested_at.desc())
        .limit(25)
    ).all())
    context = _context(request, db, user, "cameras")
    context.update({
        "camera": camera,
        "config": config,
        "state": state,
        "actions": actions,
        "can_manage": user_has_permission(user, "barrier.manage"),
        "can_operate": user_has_permission(user, "barrier.operate"),
    })
    return templates.TemplateResponse(request=request, name="barriers/detail.html", context=context)


@router.post("/cameras/{camera_id}/barrier/configuration")
def save_configuration(
    camera_id: UUID,
    request: Request,
    control_mode: Annotated[str, Form()],
    feedback_mode: Annotated[str, Form()],
    confirmation_timeout: Annotated[str, Form()] = "10",
    open_input: Annotated[str, Form()] = "",
    open_level: Annotated[str, Form()] = "HIGH",
    closed_input: Annotated[str, Form()] = "",
    closed_level: Annotated[str, Form()] = "HIGH",
    notes: Annotated[str, Form()] = "",
    enabled: Annotated[str | None, Form()] = None,
    user: User = Depends(require_page_permission("barrier.manage")),
    _csrf: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    camera = _camera(db, camera_id)
    try:
        config, _created = BarrierConfigurationService(db).save(
            camera,
            enabled=enabled == "true",
            control_mode=control_mode,
            feedback_mode=feedback_mode,
            confirmation_timeout=confirmation_timeout,
            open_input=open_input,
            open_level=open_level,
            closed_input=closed_input,
            closed_level=closed_level,
            notes=notes,
        )
    except BarrierConfigurationError as error:
        raise HTTPException(400, str(error)) from error
    _audit(request, db, user, action="barrier.configuration_update", camera=camera, details={
        "enabled": config.enabled,
        "control_mode": config.control_mode,
        "feedback_mode": config.feedback_mode,
    })
    db.commit()
    if config.feedback_mode == "GPIO_INPUTS" and mqtt_heartbeat_service.is_connected:
        try:
            mqtt_heartbeat_service.refresh_barrier_status(camera.id)
        except RuntimeError:
            pass
    return RedirectResponse(f"/cameras/{camera.id}/barrier", status_code=303)


@router.post("/cameras/{camera_id}/barrier/commission")
def commission(
    camera_id: UUID,
    request: Request,
    confirmation: Annotated[str, Form()],
    notes: Annotated[str, Form()] = "",
    local_auto_open_verified: Annotated[str | None, Form()] = None,
    user: User = Depends(require_page_permission("barrier.manage")),
    _csrf: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    camera = _camera(db, camera_id)
    config = BarrierConfigurationService(db).get(camera.id)
    if config is None:
        raise HTTPException(409, "Save the barrier configuration before commissioning.")
    if confirmation.strip().upper() != "CENTRAL MANAGED":
        raise HTTPException(400, "Type CENTRAL MANAGED to confirm commissioning.")
    try:
        BarrierConfigurationService(db).commission(
            config,
            user.id,
            local_auto_open_verified=local_auto_open_verified == "true",
            notes=notes,
        )
    except BarrierConfigurationError as error:
        raise HTTPException(409, str(error)) from error
    _audit(request, db, user, action="barrier.commission", camera=camera, details={"control_mode": config.control_mode})
    db.commit()
    return RedirectResponse(f"/cameras/{camera.id}/barrier", status_code=303)


@router.post("/cameras/{camera_id}/barrier/decommission")
def decommission(
    camera_id: UUID,
    request: Request,
    user: User = Depends(require_page_permission("barrier.manage")),
    _csrf: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    camera = _camera(db, camera_id)
    config = BarrierConfigurationService(db).get(camera.id)
    if config is None:
        raise HTTPException(404, "Barrier configuration not found")
    BarrierConfigurationService(db).decommission(config)
    _audit(request, db, user, action="barrier.decommission", camera=camera)
    db.commit()
    return RedirectResponse(f"/cameras/{camera.id}/barrier", status_code=303)


@router.post("/cameras/{camera_id}/barrier/test-open")
def test_open(
    camera_id: UUID,
    request: Request,
    confirmation: Annotated[str, Form()],
    reason: Annotated[str, Form()] = "Commissioning gate-open test",
    user: User = Depends(require_page_permission("barrier.operate")),
    _csrf: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    camera = _camera(db, camera_id)
    if confirmation.strip().upper() != "OPEN GATE":
        raise HTTPException(400, "Type OPEN GATE to confirm the physical test.")
    try:
        action_id = mqtt_heartbeat_service.test_barrier_open(camera_id=camera.id, user_id=user.id, reason=reason)
    except (RuntimeError, BarrierActionError) as error:
        raise HTTPException(409, str(error)) from error
    _audit(request, db, user, action="barrier.test_open", camera=camera, details={"barrier_action_id": str(action_id), "reason": reason.strip()[:500]})
    db.commit()
    return RedirectResponse(f"/cameras/{camera.id}/barrier", status_code=303)


@router.post("/cameras/{camera_id}/barrier/refresh")
def refresh_state(
    camera_id: UUID,
    request: Request,
    user: User = Depends(require_page_permission("barrier.manage")),
    _csrf: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    camera = _camera(db, camera_id)
    try:
        count = mqtt_heartbeat_service.refresh_barrier_status(camera.id)
    except (RuntimeError, BarrierActionError) as error:
        raise HTTPException(409, str(error)) from error
    _audit(request, db, user, action="barrier.feedback_refresh", camera=camera, details={"queries_queued": count})
    db.commit()
    return RedirectResponse(f"/cameras/{camera.id}/barrier", status_code=303)


@router.post("/parking/reviews/{item_id}/open-barrier")
def manual_release(
    item_id: UUID,
    request: Request,
    reason: Annotated[str, Form()],
    confirmation: Annotated[str, Form()],
    user: User = Depends(require_page_permission("barrier.operate")),
    _csrf: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    review = db.scalar(
        select(ParkingReviewItem)
        .options(joinedload(ParkingReviewItem.access_decision))
        .where(ParkingReviewItem.id == item_id)
    )
    if review is None:
        raise HTTPException(404, "Review item not found")
    if confirmation.strip().upper() != "OPEN GATE":
        raise HTTPException(400, "Type OPEN GATE to confirm the manual release.")
    try:
        action_id = mqtt_heartbeat_service.manual_barrier_open(
            camera_id=review.access_decision.camera_id,
            user_id=user.id,
            reason=reason,
            review_item_id=review.id,
        )
    except (RuntimeError, BarrierActionError) as error:
        raise HTTPException(409, str(error)) from error
    camera = _camera(db, review.access_decision.camera_id)
    _audit(request, db, user, action="barrier.manual_open", camera=camera, details={
        "barrier_action_id": str(action_id),
        "review_item_id": str(review.id),
        "reason": reason.strip()[:500],
    })
    db.commit()
    return RedirectResponse("/parking/reviews", status_code=303)


@router.get("/barrier-actions", response_class=HTMLResponse)
def action_history(
    request: Request,
    user: User = Depends(require_page_permission("parking.view")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    actions = tuple(db.scalars(
        select(BarrierAction)
        .options(joinedload(BarrierAction.camera), joinedload(BarrierAction.site), joinedload(BarrierAction.requested_by), joinedload(BarrierAction.recognition_event), joinedload(BarrierAction.access_decision))
        .order_by(BarrierAction.requested_at.desc())
        .limit(250)
    ).unique().all())
    context = _context(request, db, user, "barrier-actions")
    context["actions"] = actions
    return templates.TemplateResponse(request=request, name="barriers/actions.html", context=context)


__all__ = ["router"]
