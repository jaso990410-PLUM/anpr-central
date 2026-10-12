from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.db.dependencies import get_db
from app.dependencies.auth import require_page_permission, user_has_permission
from app.dependencies.csrf import require_session_csrf
from app.models.access_list_entry import AccessListEntry
from app.models.parking import AccessProfile
from app.models.residential import ResidentialUnit, Resident, ResidentUnitMembership, ResidentVehicleAssignment
from app.models.user import User
from app.models.recognition_event import RecognitionEvent
from app.services.plate_correction_service import effective_plate
from app.services.audit_service import create_audit_log
from app.services.dashboard_service import get_dashboard_summary
from app.services.mqtt_service import mqtt_heartbeat_service
from app.services.residential_identity_service import ResidentialIdentityService, ResidentialValidationError
from app.services.access_list_service import AccessListService, AccessListValidationError
from app.services.shell_service import build_navigation, role_label
from app.services.visitor_reservation_service import VisitorReservationService, VisitorValidationError
from app.web import templates


router = APIRouter(prefix="/residential")


def _context(request: Request, db: Session, user: User, active_page: str) -> dict[str, object]:
    raw = request.cookies[settings.session_cookie_name]
    return {"request": request, "user": user, "csrf_token": generate_csrf_token(raw),
            "summary": get_dashboard_summary(db), "active_page": active_page,
            "navigation": build_navigation(user), "user_role_label": role_label(user),
            "can_manage_residential": user_has_permission(user, "residential.manage"),
            "can_manage_visitors": user_has_permission(user, "visitor.manage"),
            "can_approve_visitors": user_has_permission(user, "visitor.approve"),
            "can_manage_access": user_has_permission(user, "access_list.manage")}


def _audit(db: Session, request: Request, user: User, action: str, entity_type: str,
           entity_id: UUID, site_id: UUID | None = None, details=None) -> None:
    create_audit_log(db, action=action, actor_user_id=user.id, site_id=site_id,
                     entity_type=entity_type, entity_id=str(entity_id), details=details,
                     ip_address=request.client.host if request.client else None)


def _redirect(path: str, message: str | None = None, error: str | None = None) -> RedirectResponse:
    joiner = "&" if "?" in path else "?"
    suffix = f"{joiner}message={quote(message)}" if message else (f"{joiner}error={quote(error)}" if error else "")
    return RedirectResponse(path + suffix, status_code=303)


@router.get("", response_class=HTMLResponse)
def overview(request: Request, user: User = Depends(require_page_permission("residential.view")), db: Session = Depends(get_db)):
    context = _context(request, db, user, "residential")
    context.update({"unit_count": db.scalar(select(func.count()).select_from(ResidentialUnit)) or 0,
                    "resident_count": db.scalar(select(func.count()).select_from(Resident)) or 0,
                    "visitor_count": len(VisitorReservationService(db).list())})
    return templates.TemplateResponse(request=request, name="residential/overview.html", context=context)


@router.get("/units", response_class=HTMLResponse)
def units(request: Request, site: str | None = None, user: User = Depends(require_page_permission("residential.view")), db: Session = Depends(get_db)):
    service = ResidentialIdentityService(db); site_id = UUID(site) if site else None
    context = _context(request, db, user, "residential-units")
    context.update({"units": service.units(site_id), "sites": service.sites(), "selected_site": site or ""})
    return templates.TemplateResponse(request=request, name="residential/units.html", context=context)


@router.post("/units")
def create_unit(request: Request, site_id: Annotated[UUID, Form()], unit_code: Annotated[str, Form()],
                display_name: Annotated[str, Form()] = "", block: Annotated[str, Form()] = "",
                floor: Annotated[str, Form()] = "", notes: Annotated[str, Form()] = "",
                user: User = Depends(require_page_permission("residential.manage")),
                _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    try: unit = ResidentialIdentityService(db).create_unit(site_id=site_id, unit_code=unit_code, display_name=display_name, block=block, floor=floor, notes=notes)
    except ResidentialValidationError as error: return _redirect("/residential/units", error=str(error))
    _audit(db, request, user, "residential.unit_create", "residential_unit", unit.id, site_id)
    db.commit(); return _redirect(f"/residential/units/{unit.id}", "Unit created")


@router.get("/units/{unit_id}", response_class=HTMLResponse)
def unit_detail(unit_id: UUID, request: Request, user: User = Depends(require_page_permission("residential.view")), db: Session = Depends(get_db)):
    service = ResidentialIdentityService(db); unit = service.unit(unit_id)
    if unit is None: raise HTTPException(404, "Residential unit not found")
    available_entries = tuple(db.scalars(select(AccessListEntry).where(AccessListEntry.site_id == unit.site_id).order_by(AccessListEntry.created_at.desc())).all())
    context = _context(request, db, user, "residential-units")
    context.update({"unit": unit, "residents": service.residents(), "available_entries": available_entries})
    return templates.TemplateResponse(request=request, name="residential/unit_detail.html", context=context)


@router.post("/units/{unit_id}/status")
def unit_status(unit_id: UUID, request: Request, active: Annotated[bool, Form()] = False,
                disable_access: Annotated[bool, Form()] = False,
                user: User = Depends(require_page_permission("residential.manage")),
                _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    service = ResidentialIdentityService(db); unit = service.unit(unit_id)
    if unit is None: raise HTTPException(404, "Residential unit not found")
    changed = service.set_unit_active(unit, active, disable_access=disable_access)
    _audit(db, request, user, "residential.unit_activate" if active else "residential.unit_deactivate", "residential_unit", unit.id, unit.site_id, {"disable_linked_access": disable_access})
    db.commit()
    for entry_id in changed: mqtt_heartbeat_service.enqueue_access_entry(entry_id)
    return _redirect(f"/residential/units/{unit.id}", "Unit status updated")


@router.post("/units/{unit_id}")
def update_unit(unit_id: UUID, request: Request, unit_code: Annotated[str, Form()], display_name: Annotated[str, Form()] = "",
                block: Annotated[str, Form()] = "", floor: Annotated[str, Form()] = "", notes: Annotated[str, Form()] = "",
                user: User = Depends(require_page_permission("residential.manage")), _csrf: str = Depends(require_session_csrf),
                db: Session = Depends(get_db)):
    service = ResidentialIdentityService(db); unit = service.unit(unit_id)
    if unit is None: raise HTTPException(404, "Residential unit not found")
    try: service.update_unit(unit, unit_code=unit_code, display_name=display_name, block=block, floor=floor, notes=notes)
    except ResidentialValidationError as error: return _redirect(f"/residential/units/{unit_id}", error=str(error))
    _audit(db, request, user, "residential.unit_update", "residential_unit", unit.id, unit.site_id); db.commit()
    return _redirect(f"/residential/units/{unit.id}", "Unit updated")


@router.post("/units/{unit_id}/memberships")
def add_membership(unit_id: UUID, request: Request, resident_id: Annotated[UUID, Form()],
                   membership_type: Annotated[str, Form()], primary: Annotated[bool, Form()] = False,
                   user: User = Depends(require_page_permission("residential.manage")),
                   _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    try: item = ResidentialIdentityService(db).add_membership(resident_id=resident_id, unit_id=unit_id, membership_type=membership_type, primary=primary)
    except ResidentialValidationError as error: return _redirect(f"/residential/units/{unit_id}", error=str(error))
    _audit(db, request, user, "residential.membership_create", "resident_unit_membership", item.id)
    db.commit(); return _redirect(f"/residential/units/{unit_id}", "Resident linked")


@router.post("/memberships/{membership_id}/end")
def end_membership(membership_id: UUID, request: Request, user: User = Depends(require_page_permission("residential.manage")),
                   _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    item = db.get(ResidentUnitMembership, membership_id)
    if item is None: raise HTTPException(404, "Membership not found")
    ResidentialIdentityService(db).end_membership(item); _audit(db, request, user, "residential.membership_end", "resident_unit_membership", item.id)
    db.commit(); return _redirect(f"/residential/units/{item.unit_id}", "Membership ended")


@router.post("/units/{unit_id}/vehicles")
def link_vehicle(unit_id: UUID, request: Request, access_entry_id: Annotated[UUID, Form()],
                 resident_id: Annotated[str, Form()] = "", label: Annotated[str, Form()] = "",
                 user: User = Depends(require_page_permission("residential.manage")),
                 _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    try: item = ResidentialIdentityService(db).link_vehicle(unit_id=unit_id, access_entry_id=access_entry_id, resident_id=UUID(resident_id) if resident_id else None, label=label)
    except (ResidentialValidationError, ValueError) as error: return _redirect(f"/residential/units/{unit_id}", error=str(error))
    _audit(db, request, user, "residential.vehicle_link", "resident_vehicle_assignment", item.id)
    db.commit(); return _redirect(f"/residential/units/{unit_id}", "Vehicle linked without changing authorization")


@router.post("/units/{unit_id}/vehicles/new")
def create_and_link_vehicle(unit_id: UUID, request: Request, plate: Annotated[str, Form()],
                            access_profile_id: Annotated[str, Form()] = "", resident_id: Annotated[str, Form()] = "",
                            label: Annotated[str, Form()] = "", user: User = Depends(require_page_permission("residential.manage")),
                            _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    if not user_has_permission(user, "access_list.manage"):
        raise HTTPException(403, "Access List management permission is required to create authorization")
    identity = ResidentialIdentityService(db); unit = identity.unit(unit_id)
    if unit is None: raise HTTPException(404, "Residential unit not found")
    settings_item = identity.settings(unit.site_id)
    profile_id = UUID(access_profile_id) if access_profile_id else (settings_item.default_resident_access_profile_id if settings_item else None)
    if profile_id is None: return _redirect(f"/residential/units/{unit_id}", error="Select a resident access profile or configure the site default")
    try:
        entry = AccessListService(db).create(plate=plate, site_id=unit.site_id, reference_name=label,
            list_type="ALLOW", enabled=True, daily_window_enabled=False, camera_comment="RESIDENT",
            access_profile_id=profile_id, max_stay_minutes_override=None)
        assignment = identity.link_vehicle(unit_id=unit.id, access_entry_id=entry.id,
            resident_id=UUID(resident_id) if resident_id else None, label=label)
    except (AccessListValidationError, ResidentialValidationError, ValueError) as error:
        db.rollback(); return _redirect(f"/residential/units/{unit_id}", error=str(error))
    _audit(db, request, user, "residential.vehicle_create", "resident_vehicle_assignment", assignment.id, unit.site_id,
           {"access_entry_id": str(entry.id), "plate": entry.vehicle.plate_text})
    db.commit(); mqtt_heartbeat_service.enqueue_access_entry(entry.id)
    return _redirect(f"/residential/units/{unit.id}", "Resident vehicle created through Central Access")


@router.post("/vehicles/{assignment_id}/unlink")
def unlink_vehicle(assignment_id: UUID, request: Request, disable_access: Annotated[bool, Form()] = False,
                   user: User = Depends(require_page_permission("residential.manage")),
                   _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    item = db.get(ResidentVehicleAssignment, assignment_id)
    if item is None: raise HTTPException(404, "Vehicle assignment not found")
    unit_id = item.unit_id; changed = ResidentialIdentityService(db).unlink_vehicle(item, disable_access=disable_access)
    _audit(db, request, user, "residential.vehicle_unlink", "resident_vehicle_assignment", item.id, details={"disable_access": disable_access})
    db.commit()
    if changed: mqtt_heartbeat_service.enqueue_access_entry(changed)
    return _redirect(f"/residential/units/{unit_id}", "Vehicle identity link removed")


@router.get("/residents", response_class=HTMLResponse)
def residents(request: Request, user: User = Depends(require_page_permission("residential.view")), db: Session = Depends(get_db)):
    context = _context(request, db, user, "residential-residents"); context["residents"] = ResidentialIdentityService(db).residents()
    return templates.TemplateResponse(request=request, name="residential/residents.html", context=context)


@router.post("/residents")
def create_resident(request: Request, display_name: Annotated[str, Form()], phone: Annotated[str, Form()] = "",
                    email: Annotated[str, Form()] = "", notes: Annotated[str, Form()] = "",
                    user: User = Depends(require_page_permission("residential.manage")),
                    _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    try: resident = ResidentialIdentityService(db).create_resident(display_name=display_name, phone=phone, email=email, notes=notes)
    except ResidentialValidationError as error: return _redirect("/residential/residents", error=str(error))
    _audit(db, request, user, "residential.resident_create", "resident", resident.id); db.commit()
    return _redirect(f"/residential/residents/{resident.id}", "Resident created")


@router.get("/residents/{resident_id}", response_class=HTMLResponse)
def resident_detail(resident_id: UUID, request: Request, user: User = Depends(require_page_permission("residential.view")), db: Session = Depends(get_db)):
    resident = ResidentialIdentityService(db).resident(resident_id)
    if resident is None: raise HTTPException(404, "Resident not found")
    context = _context(request, db, user, "residential-residents"); context["resident"] = resident
    return templates.TemplateResponse(request=request, name="residential/resident_detail.html", context=context)


@router.post("/residents/{resident_id}")
def update_resident(resident_id: UUID, request: Request, display_name: Annotated[str, Form()], phone: Annotated[str, Form()] = "",
                    email: Annotated[str, Form()] = "", notes: Annotated[str, Form()] = "",
                    user: User = Depends(require_page_permission("residential.manage")),
                    _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    service = ResidentialIdentityService(db); resident = service.resident(resident_id)
    if resident is None: raise HTTPException(404, "Resident not found")
    try: service.update_resident(resident, display_name=display_name, phone=phone, email=email, notes=notes)
    except ResidentialValidationError as error: return _redirect(f"/residential/residents/{resident_id}", error=str(error))
    _audit(db, request, user, "residential.resident_update", "resident", resident.id); db.commit()
    return _redirect(f"/residential/residents/{resident.id}", "Resident updated")


@router.post("/residents/{resident_id}/status")
def resident_status(resident_id: UUID, request: Request, active: Annotated[bool, Form()] = False,
                    disable_access: Annotated[bool, Form()] = False,
                    user: User = Depends(require_page_permission("residential.manage")),
                    _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    service = ResidentialIdentityService(db); resident = service.resident(resident_id)
    if resident is None: raise HTTPException(404, "Resident not found")
    changed = service.set_resident_active(resident, active, disable_access=disable_access)
    _audit(db, request, user, "residential.resident_activate" if active else "residential.resident_deactivate", "resident", resident.id, details={"disable_linked_access": disable_access})
    db.commit()
    for entry_id in changed: mqtt_heartbeat_service.enqueue_access_entry(entry_id)
    return _redirect(f"/residential/residents/{resident.id}", "Resident status updated")


@router.get("/visitors", response_class=HTMLResponse)
def visitors(request: Request, status: str | None = None, user: User = Depends(require_page_permission("residential.view")), db: Session = Depends(get_db)):
    context = _context(request, db, user, "residential-visitors"); context["visitors"] = VisitorReservationService(db).list(status=status); context["selected_status"] = status or ""
    return templates.TemplateResponse(request=request, name="residential/visitors.html", context=context)


@router.get("/visitors/new", response_class=HTMLResponse)
def visitor_new(request: Request, user: User = Depends(require_page_permission("visitor.manage")), db: Session = Depends(get_db)):
    identity = ResidentialIdentityService(db); context = _context(request, db, user, "residential-visitors")
    context.update({"units": identity.units(), "residents": identity.residents(), "sites": identity.sites(),
                    "profiles": tuple(db.scalars(select(AccessProfile).where(AccessProfile.enabled.is_(True)).order_by(AccessProfile.name)).all())})
    return templates.TemplateResponse(request=request, name="residential/visitor_form.html", context=context)


@router.post("/visitors")
def visitor_create(request: Request, site_id: Annotated[UUID, Form()], unit_id: Annotated[UUID, Form()],
                   visitor_name: Annotated[str, Form()], valid_from: Annotated[str, Form()], valid_until: Annotated[str, Form()],
                   visitor_phone: Annotated[str, Form()] = "", purpose: Annotated[str, Form()] = "", plate: Annotated[str, Form()] = "",
                   host_resident_id: Annotated[str, Form()] = "", access_profile_id: Annotated[str, Form()] = "",
                   expected_arrival_at: Annotated[str, Form()] = "", visit_mode: Annotated[str, Form()] = "",
                   max_stay_minutes_override: Annotated[str, Form()] = "",
                   user: User = Depends(require_page_permission("visitor.manage")),
                   _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    try:
        zone = _site_zone(db, site_id)
        reservation = VisitorReservationService(db).create(site_id=site_id, unit_id=unit_id, visitor_name=visitor_name,
            visitor_phone=visitor_phone, purpose=purpose, plate=plate,
            access_profile_id=UUID(access_profile_id) if access_profile_id else None,
            max_stay_minutes_override=int(max_stay_minutes_override) if max_stay_minutes_override else None,
            valid_from=_local_datetime(valid_from, zone), valid_until=_local_datetime(valid_until, zone),
            expected_arrival_at=_local_datetime(expected_arrival_at, zone) if expected_arrival_at else None,
            visit_mode=visit_mode or None, host_resident_id=UUID(host_resident_id) if host_resident_id else None,
            requested_by_user_id=user.id)
    except (VisitorValidationError, ValueError) as error: return _redirect("/residential/visitors/new", error=str(error))
    _audit(db, request, user, "visitor.create", "visitor_reservation", reservation.id, site_id); db.commit()
    return _redirect(f"/residential/visitors/{reservation.id}", "Visitor reservation created")


@router.get("/visitors/{reservation_id}", response_class=HTMLResponse)
def visitor_detail(reservation_id: UUID, request: Request, user: User = Depends(require_page_permission("residential.view")), db: Session = Depends(get_db)):
    item = VisitorReservationService(db).get(reservation_id)
    if item is None: raise HTTPException(404, "Visitor reservation not found")
    identity = ResidentialIdentityService(db)
    context = _context(request, db, user, "residential-visitors"); context.update({"visitor": item,
        "residents": identity.residents(),
        "profiles": tuple(db.scalars(select(AccessProfile).where(AccessProfile.site_id == item.site_id, AccessProfile.enabled.is_(True)).order_by(AccessProfile.name)).all())})
    return templates.TemplateResponse(request=request, name="residential/visitor_detail.html", context=context)


@router.post("/visitors/{reservation_id}")
def update_visitor(reservation_id: UUID, request: Request, visitor_name: Annotated[str, Form()],
                   valid_from: Annotated[str, Form()], valid_until: Annotated[str, Form()],
                   visitor_phone: Annotated[str, Form()] = "", purpose: Annotated[str, Form()] = "",
                   host_resident_id: Annotated[str, Form()] = "", access_profile_id: Annotated[str, Form()] = "",
                   expected_arrival_at: Annotated[str, Form()] = "", visit_mode: Annotated[str, Form()] = "SINGLE_VISIT",
                   max_stay_minutes_override: Annotated[str, Form()] = "",
                   user: User = Depends(require_page_permission("visitor.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    existing = VisitorReservationService(db).get(reservation_id)
    if existing is None: raise HTTPException(404, "Visitor reservation not found")
    try:
        zone = _site_zone(db, existing.site_id)
        item = VisitorReservationService(db).update_pending(reservation_id, visitor_name=visitor_name, visitor_phone=visitor_phone,
            purpose=purpose, valid_from=_local_datetime(valid_from, zone), valid_until=_local_datetime(valid_until, zone),
            expected_arrival_at=_local_datetime(expected_arrival_at, zone) if expected_arrival_at else None,
            visit_mode=visit_mode, access_profile_id=UUID(access_profile_id) if access_profile_id else None,
            max_stay_minutes_override=int(max_stay_minutes_override) if max_stay_minutes_override else None,
            host_resident_id=UUID(host_resident_id) if host_resident_id else None)
    except (VisitorValidationError, ValueError) as error:
        db.rollback(); return _redirect(f"/residential/visitors/{reservation_id}", error=str(error))
    _audit(db, request, user, "visitor.update", "visitor_reservation", item.id, item.site_id); db.commit()
    return _redirect(f"/residential/visitors/{item.id}", "Visitor reservation updated")


def _visitor_action(request: Request, db: Session, user: User, reservation_id: UUID, action: str, reason: str = "", plate: str = ""):
    service = VisitorReservationService(db)
    try:
        if action == "approve": item, changed = service.approve(reservation_id, actor_user_id=user.id)
        elif action == "reject": item, changed = service.reject(reservation_id, actor_user_id=user.id, reason=reason), None
        elif action == "cancel": item, changed = service.cancel(reservation_id, actor_user_id=user.id, reason=reason)
        else: item, changed = service.assign_plate(reservation_id, plate=plate)
    except VisitorValidationError as error:
        db.rollback(); return _redirect(f"/residential/visitors/{reservation_id}", error=str(error))
    _audit(db, request, user, f"visitor.{action}", "visitor_reservation", item.id, item.site_id,
           {"access_binding_mode": item.access_binding_mode, "access_entry_id": str(item.access_list_entry_id) if item.access_list_entry_id else None})
    db.commit()
    if changed: mqtt_heartbeat_service.enqueue_access_entry(changed)
    return _redirect(f"/residential/visitors/{item.id}", f"Visitor {action} completed")


@router.post("/visitors/{reservation_id}/approve")
def approve_visitor(reservation_id: UUID, request: Request, user: User = Depends(require_page_permission("visitor.approve")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    return _visitor_action(request, db, user, reservation_id, "approve")


@router.post("/visitors/{reservation_id}/reject")
def reject_visitor(reservation_id: UUID, request: Request, reason: Annotated[str, Form()], user: User = Depends(require_page_permission("visitor.approve")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    return _visitor_action(request, db, user, reservation_id, "reject", reason=reason)


@router.post("/visitors/{reservation_id}/cancel")
def cancel_visitor(reservation_id: UUID, request: Request, reason: Annotated[str, Form()], user: User = Depends(require_page_permission("visitor.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    return _visitor_action(request, db, user, reservation_id, "cancel", reason=reason)


@router.post("/visitors/{reservation_id}/plate")
def assign_plate(reservation_id: UUID, request: Request, plate: Annotated[str, Form()], user: User = Depends(require_page_permission("visitor.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    return _visitor_action(request, db, user, reservation_id, "assign_plate", plate=plate)


@router.post("/visitors/{reservation_id}/assign-from-event/{event_id}")
def assign_plate_from_event(reservation_id: UUID, event_id: UUID, request: Request,
                            user: User = Depends(require_page_permission("visitor.manage")),
                            _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    event = db.get(RecognitionEvent, event_id)
    if event is None: raise HTTPException(404, "Recognition event not found")
    result = _visitor_action(request, db, user, reservation_id, "assign_plate", plate=effective_plate(db, event))
    if isinstance(result, RedirectResponse) and "error=" not in result.headers.get("location", ""):
        return RedirectResponse(f"/operations/events/{event_id}?message=Visitor+plate+assigned", status_code=303)
    return result


@router.get("/settings", response_class=HTMLResponse)
def residential_settings(request: Request, site: str | None = None, user: User = Depends(require_page_permission("residential.manage")), db: Session = Depends(get_db)):
    service = ResidentialIdentityService(db); sites = service.sites(); selected = UUID(site) if site else (sites[0].id if sites else None)
    context = _context(request, db, user, "residential-settings")
    context.update({"sites": sites, "selected_site": selected, "settings_item": service.settings(selected) if selected else None,
                    "profiles": tuple(db.scalars(select(AccessProfile).where(AccessProfile.site_id == selected, AccessProfile.enabled.is_(True)).order_by(AccessProfile.name)).all()) if selected else ()})
    return templates.TemplateResponse(request=request, name="residential/settings.html", context=context)


@router.post("/settings")
def save_settings(request: Request, site_id: Annotated[UUID, Form()], resident_profile_id: Annotated[str, Form()] = "",
                  visitor_profile_id: Annotated[str, Form()] = "", default_visitor_visit_mode: Annotated[str, Form()] = "SINGLE_VISIT",
                  user: User = Depends(require_page_permission("residential.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    try: item = ResidentialIdentityService(db).save_settings(site_id=site_id,
        resident_profile_id=UUID(resident_profile_id) if resident_profile_id else None,
        visitor_profile_id=UUID(visitor_profile_id) if visitor_profile_id else None, visit_mode=default_visitor_visit_mode)
    except (ResidentialValidationError, ValueError) as error: return _redirect(f"/residential/settings?site={site_id}", error=str(error))
    _audit(db, request, user, "residential.settings_update", "residential_site_settings", site_id, site_id,
           {"default_resident_profile_id": resident_profile_id or None, "default_visitor_profile_id": visitor_profile_id or None, "default_visit_mode": default_visitor_visit_mode})
    db.commit(); return _redirect(f"/residential/settings?site={site_id}", "Residential settings saved")


def _site_zone(db: Session, site_id: UUID):
    from app.models.site import Site
    site = db.get(Site, site_id)
    if site is None: raise VisitorValidationError("Select a valid site")
    try: return ZoneInfo(site.timezone)
    except ZoneInfoNotFoundError: return timezone.utc


def _local_datetime(value: str, zone) -> datetime:
    try: parsed = datetime.fromisoformat(value)
    except ValueError as error: raise VisitorValidationError("Enter a valid date and time") from error
    if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(timezone.utc)
