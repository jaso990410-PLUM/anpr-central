from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urlencode
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.navigation import safe_local_redirect
from app.core.csrf import generate_csrf_token
from app.db.dependencies import get_db
from app.dependencies.auth import require_page_permission, require_permission, user_has_permission
from app.dependencies.csrf import require_session_csrf
from app.models.access_list_entry import AccessListEntry
from app.models.recognition_event import RecognitionEvent
from app.models.user import User
from app.models.parking import AccessProfile
from app.models.residential import ResidentVehicleAssignment, VisitorReservation
from sqlalchemy import select
from app.services.access_list_service import (
    AccessListService,
    AccessListValidationError,
    entry_summary,
    display_datetime,
    overall_sync_status,
)
from app.services.audit_service import create_audit_log
from app.services.dashboard_service import get_dashboard_summary
from app.services.mqtt_service import mqtt_heartbeat_service
from app.services.shell_service import build_navigation, role_label
from app.web import templates


router = APIRouter(prefix="/access-list")
SYNC_STATES = (
    "PENDING", "SYNCING", "ACKNOWLEDGED", "VERIFIED", "PARTIALLY_VERIFIED",
    "MISMATCH", "NOT_FOUND", "TIMEOUT_UNCERTAIN", "CAMERA_OFFLINE", "ERROR",
    "OUT_OF_SYNC",
)


def _context(request: Request, db: Session, user: User) -> dict[str, object]:
    raw = request.cookies[settings.session_cookie_name]
    return {
        "request": request,
        "user": user,
        "csrf_token": generate_csrf_token(raw),
        "summary": get_dashboard_summary(db),
        "active_page": "access-list",
        "navigation": build_navigation(user),
        "user_role_label": role_label(user),
        "can_manage": user_has_permission(user, "access_list.manage"),
        "live_poll_seconds": settings.live_poll_seconds,
    }


def _entry_or_404(service: AccessListService, entry_id: UUID) -> AccessListEntry:
    entry = service.get(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Access-list entry not found")
    return entry


def _form_values(form: dict[str, object]) -> dict[str, object]:
    maximum_stay = form.get("max_stay_minutes_override") or ""
    override_mode = str(form.get("max_stay_override_mode") or "").strip().upper()
    if override_mode:
        if override_mode == "PROFILE":
            maximum_stay = ""
        elif override_mode == "CUSTOM":
            raw_value = str(form.get("max_stay_override_value") or "").strip()
            unit = str(form.get("max_stay_override_unit") or "MINUTES").strip().upper()
            if unit not in {"MINUTES", "HOURS", "DAYS"}:
                raise AccessListValidationError("Select a valid maximum-stay unit")
            try:
                amount = int(raw_value)
            except ValueError as error:
                raise AccessListValidationError("Enter a whole-number maximum-stay override") from error
            if amount <= 0:
                raise AccessListValidationError("Maximum-stay override must be greater than zero")
            maximum_stay = str(amount * {"MINUTES": 1, "HOURS": 60, "DAYS": 1440}[unit])
        else:
            raise AccessListValidationError("Select profile default or a custom maximum stay")
    return {
        "list_type": form.get("list_type", "ALLOW"),
        "enabled": form.get("enabled") == "true" or form.get("enabled") is True,
        "valid_from": form.get("valid_from") or "",
        "valid_until": form.get("valid_until") or "",
        "daily_window_enabled": form.get("daily_window_enabled") == "true" or form.get("daily_window_enabled") is True,
        "daily_start": form.get("daily_start") or "",
        "daily_end": form.get("daily_end") or "",
        "camera_comment": form.get("camera_comment") or "",
        "access_profile_id": form.get("access_profile_id") or "",
        "max_stay_minutes_override": maximum_stay,
    }


def _submitted_values(form: dict[str, object]) -> dict[str, object]:
    legacy = str(form.get("max_stay_minutes_override") or "").strip()
    return {
        "list_type": form.get("list_type", "ALLOW"),
        "enabled": form.get("enabled") == "true" or form.get("enabled") is True,
        "valid_from": form.get("valid_from") or "",
        "valid_until": form.get("valid_until") or "",
        "daily_window_enabled": form.get("daily_window_enabled") == "true" or form.get("daily_window_enabled") is True,
        "daily_start": form.get("daily_start") or "",
        "daily_end": form.get("daily_end") or "",
        "camera_comment": form.get("camera_comment") or "",
        "access_profile_id": form.get("access_profile_id") or "",
        "max_stay_minutes_override": legacy,
        "max_stay_override_mode": form.get("max_stay_override_mode") or ("CUSTOM" if legacy else "PROFILE"),
        "max_stay_override_value": form.get("max_stay_override_value") or legacy,
        "max_stay_override_unit": form.get("max_stay_override_unit") or "MINUTES",
        "return_to": form.get("return_to") or "",
    }


def _entry_values(entry: AccessListEntry) -> dict[str, object]:
    try:
        zone = ZoneInfo(entry.site.timezone)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    def local(value: datetime | None) -> str:
        if value is None:
            return ""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(zone).strftime("%Y-%m-%dT%H:%M")
    override = entry.max_stay_minutes_override
    if override and override % 1440 == 0:
        override_value, override_unit = override // 1440, "DAYS"
    elif override and override % 60 == 0:
        override_value, override_unit = override // 60, "HOURS"
    else:
        override_value, override_unit = override or "", "MINUTES"
    return {
        "reference_name": entry.vehicle.reference_name or "",
        "list_type": entry.list_type,
        "enabled": entry.enabled,
        "valid_from": local(entry.valid_from),
        "valid_until": local(entry.valid_until),
        "daily_window_enabled": entry.daily_window_enabled,
        "daily_start": entry.daily_start.strftime("%H:%M") if entry.daily_start else "",
        "daily_end": entry.daily_end.strftime("%H:%M") if entry.daily_end else "",
        "camera_comment": entry.camera_comment or "",
        "access_profile_id": str(entry.access_profile_id) if entry.access_profile_id else "",
        "max_stay_minutes_override": entry.max_stay_minutes_override or "",
        "max_stay_override_mode": "CUSTOM" if override else "PROFILE",
        "max_stay_override_value": override_value,
        "max_stay_override_unit": override_unit,
    }


@router.get("", response_class=HTMLResponse)
def access_list_index(
    request: Request,
    plate: Annotated[str | None, Query(max_length=32)] = None,
    site: Annotated[str | None, Query(max_length=36)] = None,
    list_type: str | None = None,
    enabled: str | None = None,
    sync_state: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    user: User = Depends(require_page_permission("access_list.view")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    enabled_value = None if enabled not in {"true", "false"} else enabled == "true"
    site_id = None
    if site:
        try:
            site_id = UUID(site)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Invalid site filter") from error
    service = AccessListService(db)
    result = service.list_entries(plate=plate, site_id=site_id, list_type=list_type, enabled=enabled_value, sync_status=sync_state, page=page)
    filters = {"plate": plate or "", "site": str(site_id) if site_id else "", "list_type": list_type or "", "enabled": enabled or "", "sync_state": sync_state or ""}
    def page_url(target: int) -> str:
        return "/access-list?" + urlencode({**filters, "page": target})
    context = _context(request, db, user)
    context.update({
        "result": result,
        "sites": service.sites(),
        "filters": filters,
        "sync_states": SYNC_STATES,
        "overall_sync_status": overall_sync_status,
        "display_datetime": display_datetime,
        "previous_url": page_url(result.page - 1) if result.page > 1 else None,
        "next_url": page_url(result.page + 1) if result.page < result.page_count else None,
    })
    return templates.TemplateResponse(request=request, name="access_list/list.html", context=context)


def _render_form(request: Request, db: Session, user: User, *, values: dict[str, object], error: str | None = None, status_code: int = 200) -> HTMLResponse:
    context = _context(request, db, user)
    context.update({"sites": AccessListService(db).sites(), "access_profiles": tuple(db.scalars(select(AccessProfile).where(AccessProfile.enabled.is_(True)).order_by(AccessProfile.name)).all()), "values": values, "error": error})
    return templates.TemplateResponse(request=request, name="access_list/new.html", context=context, status_code=status_code)


@router.get("/new", response_class=HTMLResponse)
def new_access_entry(
    request: Request,
    recognition_id: UUID | None = None,
    list_type: Literal["ALLOW", "BLOCK"] = "ALLOW",
    return_to: str | None = None,
    user: User = Depends(require_page_permission("access_list.manage")),
    db: Session = Depends(get_db),
) -> Response:
    values: dict[str, object] = {"list_type": list_type, "enabled": True, "max_stay_override_mode": "PROFILE", "max_stay_override_unit": "HOURS", "return_to": safe_local_redirect(return_to, default="")}
    if recognition_id is not None:
        event = db.get(RecognitionEvent, recognition_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Recognition event not found")
        service = AccessListService(db)
        existing = service.find_for_plate_site(event.plate_text, event.site_id)
        if existing is not None:
            return RedirectResponse(f"/access-list/{existing.id}", status_code=303)
        values.update({
            "plate": event.plate_text,
            "site_id": str(event.site_id),
            "recognition_id": str(event.id),
            "recognition_prefill": True,
        })
    return _render_form(request, db, user, values=values)


@router.post("/new")
def create_access_entry(
    request: Request,
    plate: Annotated[str, Form()], site_id: Annotated[str, Form()], list_type: Annotated[str, Form()],
    enabled: Annotated[str | None, Form()] = None, valid_from: Annotated[str | None, Form()] = None,
    valid_until: Annotated[str | None, Form()] = None, daily_window_enabled: Annotated[str | None, Form()] = None,
    daily_start: Annotated[str | None, Form()] = None, daily_end: Annotated[str | None, Form()] = None,
    camera_comment: Annotated[str | None, Form()] = None,
    access_profile_id: Annotated[str | None, Form()] = None,
    max_stay_minutes_override: Annotated[str | None, Form()] = None,
    max_stay_override_mode: Annotated[str | None, Form()] = None,
    max_stay_override_value: Annotated[str | None, Form()] = None,
    max_stay_override_unit: Annotated[str | None, Form()] = None,
    reference_name: Annotated[str | None, Form(max_length=120)] = None,
    recognition_id: Annotated[UUID | None, Form()] = None,
    return_to: Annotated[str | None, Form()] = None,
    user: User = Depends(require_page_permission("access_list.manage")),
    _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db),
) -> Response:
    service = AccessListService(db)
    raw = locals()
    create_plate, create_site_id = plate, site_id
    values = {"plate": plate, "site_id": site_id, "reference_name": reference_name or "", **_submitted_values(raw)}
    if recognition_id is not None:
        event = db.get(RecognitionEvent, recognition_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Recognition event not found")
        create_plate, create_site_id = event.plate_text, str(event.site_id)
        values.update({
            "plate": create_plate,
            "site_id": create_site_id,
            "recognition_id": str(event.id),
            "recognition_prefill": True,
        })
        existing = service.find_for_plate_site(create_plate, create_site_id)
        if existing is not None:
            return RedirectResponse(f"/access-list/{existing.id}", status_code=303)
    try:
        access_values = _form_values(raw)
        entry = service.create(
            plate=create_plate,
            site_id=create_site_id,
            reference_name=reference_name,
            **access_values,
        )
    except AccessListValidationError as error:
        return _render_form(request, db, user, values=values, error=str(error), status_code=400)
    create_audit_log(db, action="access_list.create", actor_user_id=user.id, site_id=entry.site_id, entity_type="access_list_entry", entity_id=str(entry.id), details=entry_summary(entry), ip_address=request.client.host if request.client else None)
    db.commit()
    mqtt_heartbeat_service.enqueue_access_entry(entry.id)
    return RedirectResponse(safe_local_redirect(return_to, default=f"/access-list/{entry.id}"), status_code=303)


@router.post("/{entry_id}/cameras/{camera_id}/delete")
def delete_camera_record(
    entry_id: UUID, camera_id: UUID, request: Request,
    user: User = Depends(require_page_permission("access_list.manage")),
    _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db),
) -> RedirectResponse:
    entry = _entry_or_404(AccessListService(db), entry_id)
    try:
        command = mqtt_heartbeat_service.delete_camera_access_record(entry_id, camera_id)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    create_audit_log(db, action="access_list.camera_delete", actor_user_id=user.id, site_id=entry.site_id, entity_type="access_list_entry", entity_id=str(entry.id), details={"camera_id": str(camera_id), "plate": entry.vehicle.plate_text, "central_record_retained": True, "command_id": str(command.id)}, ip_address=request.client.host if request.client else None)
    db.commit()
    return RedirectResponse(f"/access-list/{entry.id}", status_code=303)


@router.get("/{entry_id}", response_class=HTMLResponse)
def access_entry_detail(entry_id: UUID, request: Request, user: User = Depends(require_page_permission("access_list.view")), db: Session = Depends(get_db)) -> HTMLResponse:
    entry = _entry_or_404(AccessListService(db), entry_id)
    context = _context(request, db, user)
    context.update({"entry": entry, "values": _entry_values(entry), "access_profiles": tuple(db.scalars(select(AccessProfile).where(AccessProfile.site_id == entry.site_id, AccessProfile.enabled.is_(True)).order_by(AccessProfile.name)).all()), "overall_status": overall_sync_status(entry), "display_datetime": display_datetime, "error": None,
                    "residential_assignment": db.scalar(select(ResidentVehicleAssignment).where(ResidentVehicleAssignment.access_list_entry_id == entry.id, ResidentVehicleAssignment.active.is_(True))),
                    "visitor_bindings": tuple(db.scalars(select(VisitorReservation).where(VisitorReservation.access_list_entry_id == entry.id).order_by(VisitorReservation.created_at.desc()).limit(10)).all())})
    return templates.TemplateResponse(request=request, name="access_list/detail.html", context=context)


@router.post("/{entry_id}")
def update_access_entry(
    entry_id: UUID, request: Request, list_type: Annotated[str, Form()], enabled: Annotated[str | None, Form()] = None,
    valid_from: Annotated[str | None, Form()] = None, valid_until: Annotated[str | None, Form()] = None,
    daily_window_enabled: Annotated[str | None, Form()] = None, daily_start: Annotated[str | None, Form()] = None,
    daily_end: Annotated[str | None, Form()] = None, camera_comment: Annotated[str | None, Form()] = None,
    access_profile_id: Annotated[str | None, Form()] = None, max_stay_minutes_override: Annotated[str | None, Form()] = None,
    max_stay_override_mode: Annotated[str | None, Form()] = None,
    max_stay_override_value: Annotated[str | None, Form()] = None,
    max_stay_override_unit: Annotated[str | None, Form()] = None,
    reference_name: Annotated[str | None, Form(max_length=120)] = None,
    user: User = Depends(require_page_permission("access_list.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db),
) -> Response:
    service = AccessListService(db)
    entry = _entry_or_404(service, entry_id)
    raw = locals()
    values = {**_submitted_values(raw), "reference_name": reference_name or ""}
    try:
        access_values = _form_values(raw)
        before = service.update(entry, reference_name=reference_name, **access_values)
    except AccessListValidationError as error:
        context = _context(request, db, user)
        context.update({"entry": entry, "values": values, "access_profiles": tuple(db.scalars(select(AccessProfile).where(AccessProfile.site_id == entry.site_id, AccessProfile.enabled.is_(True)).order_by(AccessProfile.name)).all()), "overall_status": overall_sync_status(entry), "display_datetime": display_datetime, "error": str(error)})
        return templates.TemplateResponse(request=request, name="access_list/detail.html", context=context, status_code=400)
    create_audit_log(db, action="access_list.update", actor_user_id=user.id, site_id=entry.site_id, entity_type="access_list_entry", entity_id=str(entry.id), details={"before": before, "after": entry_summary(entry)}, ip_address=request.client.host if request.client else None)
    db.commit()
    mqtt_heartbeat_service.enqueue_access_entry(entry.id)
    return RedirectResponse(f"/access-list/{entry.id}", status_code=303)


@router.post("/{entry_id}/{action}")
def access_entry_action(
    entry_id: UUID, action: str, request: Request,
    user: User = Depends(require_page_permission("access_list.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db),
) -> RedirectResponse:
    if action not in {"enable", "disable", "sync", "verify"}:
        raise HTTPException(status_code=404, detail="Unknown access-list action")
    service = AccessListService(db)
    entry = _entry_or_404(service, entry_id)
    if action in {"enable", "disable"}:
        service.set_enabled(entry, action == "enable")
        audit_action = f"access_list.{action}"
    else:
        service.ensure_sync_records(entry)
        audit_action = f"access_list.{action}_requested"
    create_audit_log(db, action=audit_action, actor_user_id=user.id, site_id=entry.site_id, entity_type="access_list_entry", entity_id=str(entry.id), details={"plate": entry.vehicle.plate_text}, ip_address=request.client.host if request.client else None)
    db.commit()
    mqtt_heartbeat_service.enqueue_access_entry(entry.id, verify_only=action == "verify")
    return RedirectResponse(f"/access-list/{entry.id}", status_code=303)


@router.get("/{entry_id}/sync-status")
def access_entry_sync_status(
    entry_id: UUID,
    _user: User = Depends(require_permission("access_list.view")),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    entry = _entry_or_404(AccessListService(db), entry_id)
    return {
        "overall": overall_sync_status(entry).replace("_", " ").title(),
        "cameras": [
            {
                "id": str(sync.id),
                "status": sync.status.replace("_", " ").title(),
                "status_code": sync.status,
                "last_verified": sync.last_verified_at.isoformat() if sync.last_verified_at else None,
                "note": sync.last_error or ("Requested fields verified" if sync.status == "VERIFIED" else "Awaiting synchronization"),
            }
            for sync in entry.camera_syncs
        ],
    }
