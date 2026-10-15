from __future__ import annotations

from datetime import date
from typing import Annotated, Literal
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BeforeValidator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.db.dependencies import get_db
from app.dependencies.auth import require_page_permission, require_permission, user_has_permission
from app.dependencies.csrf import require_session_csrf
from app.models.parking import ParkingReviewItem, ParkingSessionIssue
from app.models.barrier import BarrierState
from app.models.recognition_event import RecognitionEvent
from app.models.user import User
from app.services.audit_service import create_audit_log
from app.services.barrier_action_service import BarrierActionError
from app.services.dashboard_service import get_dashboard_summary
from app.services.media_service import MediaPlaybackService, MediaValidationError
from app.services.mqtt_service import mqtt_heartbeat_service
from app.services.operations_service import OperationsService
from app.services.parking_reconciliation_service import ParkingReconciliationService, ReconciliationConflict, ReconciliationError
from app.services.parking_review_service import ParkingReviewService
from app.services.plate_correction_service import PlateCorrectionError, PlateCorrectionService
from app.services.recognition_media_service import RecognitionMediaError, RecognitionMediaService
from app.services.shell_service import build_navigation, role_label
from app.services.visitor_reservation_service import VisitorReservationService
from app.web import templates


router = APIRouter()


def _blank(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


OptionalUUID = Annotated[UUID | None, BeforeValidator(_blank), Query()]
OptionalDate = Annotated[date | None, BeforeValidator(_blank), Query()]


def _context(request: Request, db: Session, user: User) -> dict[str, object]:
    return {
        "request": request,
        "user": user,
        "csrf_token": generate_csrf_token(request.cookies[settings.session_cookie_name]),
        "summary": get_dashboard_summary(db),
        "active_page": "operations",
        "navigation": build_navigation(user),
        "user_role_label": role_label(user),
        "can_correct": user_has_permission(user, "recognition.correct"),
        "can_reconcile": user_has_permission(user, "parking.reconcile"),
        "can_watch": user_has_permission(user, "vehicle.watch"),
        "can_manage_access": user_has_permission(user, "access_list.manage"),
        "can_operate_barrier": user_has_permission(user, "barrier.operate"),
        "can_manage_reviews": user_has_permission(user, "parking.manage"),
        "can_manage_visitors": user_has_permission(user, "visitor.manage"),
        "can_view_media": user_has_permission(user, "media.view"),
        "mqtt_connected": mqtt_heartbeat_service.is_connected,
        "live_poll_seconds": settings.live_poll_seconds,
    }


def _render_operations(
    request: Request,
    db: Session,
    user: User,
    *,
    selected_event_id: UUID | None,
    site_id: UUID | None = None,
    camera_id: UUID | None = None,
    lane_role: str = "",
    decision: str = "",
    exceptions_only: bool = False,
    query_text: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
    error: str = "",
    status_code: int = 200,
    force_historical: bool = False,
) -> HTMLResponse:
    service = OperationsService(db)
    recent = service.recent(site_id=site_id, camera_id=camera_id, lane_role=lane_role, decision=decision, exceptions_only=exceptions_only, query_text=query_text, date_from=date_from, date_to=date_to, limit=15)
    if selected_event_id is None and recent:
        selected_event_id = recent[0]["id"]
    selected = service.detail(selected_event_id) if selected_event_id else None
    if selected_event_id and selected is None:
        raise HTTPException(404, "Recognition event not found")
    context = _context(request, db, user)
    context.update({
        "sites": service.sites(),
        "cameras": service.cameras(site_id),
        "recent": recent,
        "selected": selected,
        "open_candidates": service.open_session_candidates(selected["event"]) if selected and selected["event"].camera.lane_role == "EXIT" else (),
        "similar": tuple(item for item in service.similar_plates(selected["effective_plate"], site_id=selected["event"].site_id) if item.plate != selected["effective_plate"]) if selected else (),
        "previous_visits": service.previous_visits(selected["event"], limit=5) if selected else (),
        "previous_visit_count": service.previous_visit_count(selected["event"]) if selected else 0,
        "live_mode": bool(not force_historical and selected and recent and selected["event"].id == recent[0]["id"]),
        "filters": {"site": str(site_id) if site_id else "", "camera": str(camera_id) if camera_id else "", "lane_role": lane_role, "decision": decision, "exceptions_only": exceptions_only, "q": query_text, "date_from": date_from.isoformat() if date_from else "", "date_to": date_to.isoformat() if date_to else ""},
        "error": error,
        "action_token": uuid4().hex,
        "open_exception_count": db.scalar(select(func.count()).select_from(ParkingSessionIssue).where(ParkingSessionIssue.status == "OPEN")) or 0,
        "visitor_candidates": VisitorReservationService(db).candidates_without_plate(
            site_id=selected["event"].site_id,
            at=selected["event"].captured_at or selected["event"].received_at,
        ) if selected and not selected.get("visitor") else (),
    })
    return templates.TemplateResponse(request=request, name="operations/console.html", context=context, status_code=status_code)


def _selected_lane(service: OperationsService, site_id: UUID | None, camera_id: UUID | None):
    sites = service.sites()
    site = next((item for item in sites if item.id == site_id), None) if site_id else next(
        (item for item in sites if service.cameras(item.id)), sites[0] if sites else None
    )
    if site_id and site is None:
        raise HTTPException(404, "Site not found")
    cameras = service.cameras(site.id) if site else ()
    camera = next((item for item in cameras if item.id == camera_id), None) if camera_id else (cameras[0] if cameras else None)
    if camera_id and camera is None:
        raise HTTPException(404, "Lane camera not found at the selected site")
    return sites, site, cameras, camera


def _console_payload(db: Session, service: OperationsService, site, camera, *, can_view_media: bool) -> dict[str, object]:
    if site is None:
        return {"statistics": {"entries": 0, "exits": 0, "on_site": 0}, "latest": None, "recent": [], "barrier_state": "UNKNOWN"}
    statistics = service.today_statistics(site)
    if camera is None:
        return {"statistics": statistics, "latest": None, "recent": [], "barrier_state": "UNKNOWN"}
    latest_event = service.latest_live_event(site_id=site.id, camera_id=camera.id)
    latest = service.console_latest(latest_event)
    if latest and not can_view_media:
        latest["event_video_url"] = None
        latest["recording_url"] = None
    recent = service.recent(site_id=site.id, camera_id=camera.id, limit=12)
    barrier_state = db.scalar(select(BarrierState).where(BarrierState.camera_id == camera.id))
    return {
        "statistics": statistics,
        "latest": latest,
        "recent": [{**item, "id": str(item["id"])} for item in recent],
        "barrier_state": barrier_state.normalized_state if barrier_state else "UNKNOWN",
    }


def _render_live_console(request: Request, db: Session, user: User, *, site_id: UUID | None, camera_id: UUID | None) -> HTMLResponse:
    service = OperationsService(db)
    sites, site, cameras, camera = _selected_lane(service, site_id, camera_id)
    can_view_media = user_has_permission(user, "media.view")
    live_available = False
    if camera and can_view_media:
        try:
            MediaPlaybackService(db).live_source(camera.id)
            live_available = True
        except MediaValidationError:
            pass
    context = _context(request, db, user)
    context.update({
        "sites": sites,
        "selected_site": site,
        "cameras": cameras,
        "selected_camera": camera,
        "live_available": live_available,
        "console": _console_payload(db, service, site, camera, can_view_media=can_view_media),
    })
    return templates.TemplateResponse(request=request, name="operations/live_console.html", context=context)


@router.get("/operations", response_class=HTMLResponse)
def operations_console(
    request: Request,
    event: OptionalUUID = None,
    site: OptionalUUID = None,
    camera: OptionalUUID = None,
    lane_role: Annotated[str, Query(max_length=20)] = "",
    decision: Annotated[str, Query(max_length=20)] = "",
    exceptions_only: bool = False,
    q: Annotated[str, Query(max_length=120)] = "",
    date_from: OptionalDate = None,
    date_to: OptionalDate = None,
    error: Annotated[str, Query(max_length=500)] = "",
    user: User = Depends(require_page_permission("operations.view")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    history_requested = bool(event or lane_role or decision or exceptions_only or q or date_from or date_to)
    if date_from and date_to and date_from > date_to:
        return _render_operations(request, db, user, selected_event_id=event, site_id=site, camera_id=camera, lane_role=lane_role.upper(), decision=decision.upper(), exceptions_only=exceptions_only, query_text=q, date_from=date_from, date_to=date_to, error="From date must not be later than To date.", status_code=400)
    if not history_requested:
        return _render_live_console(request, db, user, site_id=site, camera_id=camera)
    return _render_operations(request, db, user, selected_event_id=event, site_id=site, camera_id=camera, lane_role=lane_role.upper(), decision=decision.upper(), exceptions_only=exceptions_only, query_text=q, date_from=date_from, date_to=date_to, error=error)


@router.get("/operations/history", response_class=HTMLResponse)
def operations_history(
    request: Request,
    event: OptionalUUID = None,
    site: OptionalUUID = None,
    camera: OptionalUUID = None,
    lane_role: Annotated[str, Query(max_length=20)] = "",
    decision: Annotated[str, Query(max_length=20)] = "",
    exceptions_only: bool = False,
    q: Annotated[str, Query(max_length=120)] = "",
    date_from: OptionalDate = None,
    date_to: OptionalDate = None,
    user: User = Depends(require_page_permission("operations.view")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if date_from and date_to and date_from > date_to:
        return _render_operations(request, db, user, selected_event_id=event, site_id=site, camera_id=camera, lane_role=lane_role.upper(), decision=decision.upper(), exceptions_only=exceptions_only, query_text=q, date_from=date_from, date_to=date_to, error="From date must not be later than To date.", status_code=400, force_historical=True)
    return _render_operations(request, db, user, selected_event_id=event, site_id=site, camera_id=camera, lane_role=lane_role.upper(), decision=decision.upper(), exceptions_only=exceptions_only, query_text=q, date_from=date_from, date_to=date_to, force_historical=True)


@router.get("/operations/events/{event_id}", response_class=HTMLResponse)
def operations_event(event_id: UUID, request: Request, user: User = Depends(require_page_permission("operations.view")), db: Session = Depends(get_db)) -> HTMLResponse:
    return _render_operations(request, db, user, selected_event_id=event_id)


@router.get("/operations/events/{event_id}/image/{kind}")
def operations_image(
    event_id: UUID,
    kind: Literal["scene", "plate"],
    _user: User = Depends(require_permission("operations.view")),
    db: Session = Depends(get_db),
) -> FileResponse:
    event = db.get(RecognitionEvent, event_id)
    if event is None:
        raise HTTPException(404, "Recognition event not found")
    reference = event.scene_image_path if kind == "scene" else event.plate_image_path
    if not reference:
        raise HTTPException(404, "Recognition image not found")
    try:
        path = RecognitionMediaService().resolve(reference)
    except RecognitionMediaError as error:
        raise HTTPException(404, "Recognition image not found") from error
    media_type = {".jpg": "image/jpeg", ".png": "image/png", ".bmp": "image/bmp"}.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type)


@router.post("/operations/events/{event_id}/correct")
def correct_plate(
    event_id: UUID,
    request: Request,
    corrected_plate: Annotated[str, Form()],
    reason: Annotated[str, Form()],
    user: User = Depends(require_page_permission("recognition.correct")),
    _csrf: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = db.get(RecognitionEvent, event_id)
    if event is None:
        raise HTTPException(404, "Recognition event not found")
    try:
        correction = PlateCorrectionService(db).correct(event_id, corrected_plate=corrected_plate, reason=reason, user_id=user.id)
    except PlateCorrectionError as error:
        return _error_redirect(event_id, str(error))
    create_audit_log(db, action="recognition.plate_corrected", actor_user_id=user.id, site_id=event.site_id, entity_type="recognition_event", entity_id=str(event.id), details={"raw_plate": event.plate_text, "corrected_plate": correction.corrected_plate_normalized, "reason": correction.reason, "supersedes_correction_id": str(correction.supersedes_correction_id) if correction.supersedes_correction_id else None}, ip_address=request.client.host if request.client else None)
    db.commit()
    return RedirectResponse(f"/operations/events/{event.id}", status_code=303)


@router.post("/operations/events/{event_id}/reconcile")
def reconcile(
    event_id: UUID,
    request: Request,
    action_type: Annotated[str, Form()],
    action_token: Annotated[str, Form()],
    reason: Annotated[str, Form()],
    session_id: Annotated[UUID | None, Form()] = None,
    secondary_event_id: Annotated[UUID | None, Form()] = None,
    review_item_id: Annotated[UUID | None, Form()] = None,
    user: User = Depends(require_page_permission("parking.reconcile")),
    _csrf: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        record, created = ParkingReconciliationService(db).apply(action_type=action_type, action_token=action_token, primary_event_id=event_id, secondary_event_id=secondary_event_id, session_id=session_id, review_item_id=review_item_id, user_id=user.id, reason=reason)
    except ReconciliationConflict as error:
        db.rollback()
        return _error_redirect(event_id, f"RECONCILIATION CONFLICT: {error}")
    except ReconciliationError as error:
        db.rollback()
        return _error_redirect(event_id, str(error))
    if created:
        create_audit_log(db, action="parking.exception_resolved_without_match" if action_type == "RESOLVE_WITHOUT_MATCH" else "parking.reconciliation_applied", actor_user_id=user.id, site_id=record.site_id, entity_type="parking_reconciliation", entity_id=str(record.id), details={"action_type": record.action_type, "event_id": str(event_id), "session_id": str(record.parking_session_id) if record.parking_session_id else None, "reason": record.reason}, ip_address=request.client.host if request.client else None)
    db.commit()
    return RedirectResponse(f"/operations/events/{event_id}", status_code=303)


@router.post("/operations/events/{event_id}/watch")
def add_watch(
    event_id: UUID,
    request: Request,
    severity: Annotated[str, Form()],
    note: Annotated[str, Form()],
    user: User = Depends(require_page_permission("vehicle.watch")),
    _csrf: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = db.get(RecognitionEvent, event_id)
    if event is None:
        raise HTTPException(404, "Recognition event not found")
    detail = OperationsService(db).detail(event.id)
    try:
        watch = OperationsService(db).add_watch(site_id=event.site_id, plate=detail["effective_plate"], severity=severity, note=note, user_id=user.id)
    except ValueError as error:
        return _error_redirect(event_id, str(error))
    create_audit_log(db, action="vehicle_watch.created", actor_user_id=user.id, site_id=event.site_id, entity_type="vehicle_watch", entity_id=str(watch.id), details={"plate": watch.plate_normalized, "severity": watch.severity, "note": watch.note}, ip_address=request.client.host if request.client else None)
    db.commit()
    return RedirectResponse(f"/operations/events/{event_id}", status_code=303)


@router.post("/operations/events/{event_id}/reviews/{review_id}/{action}")
def review_action(
    event_id: UUID,
    review_id: UUID,
    action: str,
    request: Request,
    note: Annotated[str, Form()] = "",
    user: User = Depends(require_page_permission("parking.manage")),
    _csrf: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    review = db.get(ParkingReviewItem, review_id)
    if review is None or review.recognition_event_id != event_id:
        raise HTTPException(404, "Review item not found")
    service = ParkingReviewService(db)
    if action == "ack":
        review = service.acknowledge(review.id, user.id)
        audit_action = "parking.review_acknowledge"
    elif action == "resolve":
        if len(note.strip()) < 3:
            return _error_redirect(event_id, "Enter a meaningful review resolution note.")
        review = service.resolve(review.id, user.id, note)
        audit_action = "parking.review_resolve"
    else:
        raise HTTPException(404, "Unknown review action")
    create_audit_log(db, action=audit_action, actor_user_id=user.id, site_id=review.site_id, entity_type="parking_review_item", entity_id=str(review.id), details={"note": review.resolution_note} if action == "resolve" else None, ip_address=request.client.host if request.client else None)
    db.commit()
    return RedirectResponse(f"/operations/events/{event_id}", status_code=303)


@router.get("/operations/watchlist", response_class=HTMLResponse)
def watchlist(request: Request, user: User = Depends(require_page_permission("operations.view")), db: Session = Depends(get_db)) -> HTMLResponse:
    context = _context(request, db, user)
    context["watches"] = OperationsService(db).watches()
    return templates.TemplateResponse(request=request, name="operations/watchlist.html", context=context)


@router.post("/operations/watchlist/{watch_id}/clear")
def clear_watch(watch_id: UUID, request: Request, user: User = Depends(require_page_permission("vehicle.watch")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)) -> RedirectResponse:
    try:
        watch = OperationsService(db).clear_watch(watch_id, user_id=user.id)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
    create_audit_log(db, action="vehicle_watch.cleared", actor_user_id=user.id, site_id=watch.site_id, entity_type="vehicle_watch", entity_id=str(watch.id), details={"plate": watch.plate_normalized}, ip_address=request.client.host if request.client else None)
    db.commit()
    return RedirectResponse("/operations/watchlist", status_code=303)


@router.post("/operations/events/{event_id}/open-barrier")
def open_barrier(
    event_id: UUID,
    request: Request,
    reason: Annotated[str, Form()],
    confirmation: Annotated[str, Form()],
    user: User = Depends(require_page_permission("barrier.operate")),
    _csrf: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = db.get(RecognitionEvent, event_id)
    if event is None:
        raise HTTPException(404, "Recognition event not found")
    if confirmation.strip().upper() != "OPEN GATE":
        return _error_redirect(event_id, "Type OPEN GATE to confirm the manual release.")
    review = db.scalar(select(ParkingReviewItem).where(ParkingReviewItem.recognition_event_id == event.id).order_by(ParkingReviewItem.last_triggered_at.desc()))
    try:
        action_id = mqtt_heartbeat_service.manual_barrier_open(camera_id=event.camera_id, user_id=user.id, reason=reason, review_item_id=review.id if review else None, recognition_event_id=event.id)
    except (RuntimeError, BarrierActionError) as error:
        return _error_redirect(event_id, str(error))
    create_audit_log(db, action="barrier.manual_open", actor_user_id=user.id, site_id=event.site_id, entity_type="barrier_action", entity_id=str(action_id), details={"recognition_event_id": str(event.id), "reason": reason.strip()[:500]}, ip_address=request.client.host if request.client else None)
    db.commit()
    return RedirectResponse(f"/operations/events/{event.id}", status_code=303)


@router.get("/api/operations/recent")
def operations_recent(
    site: OptionalUUID = None,
    camera: OptionalUUID = None,
    lane_role: str = "",
    decision: str = "",
    exceptions_only: bool = False,
    q: str = "",
    date_from: OptionalDate = None,
    date_to: OptionalDate = None,
    _user: User = Depends(require_permission("operations.view")),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    items = OperationsService(db).recent(site_id=site, camera_id=camera, lane_role=lane_role.upper(), decision=decision.upper(), exceptions_only=exceptions_only, query_text=q, date_from=date_from, date_to=date_to, limit=15)
    return {"events": [{**item, "id": str(item["id"])} for item in items]}


@router.get("/api/operations/console")
def operations_console_updates(
    site: UUID,
    camera: UUID,
    user: User = Depends(require_permission("operations.view")),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    service = OperationsService(db)
    _sites, selected_site, _cameras, selected_camera = _selected_lane(service, site, camera)
    return _console_payload(
        db,
        service,
        selected_site,
        selected_camera,
        can_view_media=user_has_permission(user, "media.view"),
    )


def _error_redirect(event_id: UUID, message: str) -> RedirectResponse:
    return RedirectResponse(f"/operations?{urlencode({'event': str(event_id), 'error': message[:500]})}", status_code=303)


__all__ = ["router"]
