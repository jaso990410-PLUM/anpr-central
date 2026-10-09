from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.db.dependencies import get_db
from app.dependencies.auth import require_page_permission, user_has_permission
from app.dependencies.csrf import require_session_csrf
from app.models.parking import OverstayAlert
from app.models.user import User
from app.models.camera import Camera
from sqlalchemy import select
from app.services.audit_service import create_audit_log
from app.services.dashboard_service import get_dashboard_summary
from app.services.mqtt_service import mqtt_heartbeat_service
from app.services.parking_service import DECISION_RESPONSE_STANDARD_SOURCES, ParkingService, ParkingValidationError
from app.services.parking_review_service import ParkingReviewService
from app.services.shell_service import build_navigation, role_label
from app.web import templates

router = APIRouter()


def _context(request, db, user, active_page):
    raw = request.cookies[settings.session_cookie_name]
    return {"request": request, "user": user, "csrf_token": generate_csrf_token(raw),
        "summary": get_dashboard_summary(db), "active_page": active_page, "navigation": build_navigation(user),
        "user_role_label": role_label(user), "mqtt_connected": mqtt_heartbeat_service.is_connected}


@router.get("/parking/on-site", response_class=HTMLResponse)
def on_site(request: Request, site: str | None = None, user: User = Depends(require_page_permission("parking.view")), db: Session = Depends(get_db)):
    service = ParkingService(db); site_id = UUID(site) if site else None
    context = _context(request, db, user, "parking-on-site"); context.update({"sessions": service.on_site(site_id), "alerts": service.alerts(), "sites": service.sites(), "selected_site": site or ""})
    return templates.TemplateResponse(request=request, name="parking/on_site.html", context=context)


@router.get("/parking/history", response_class=HTMLResponse)
def history(request: Request, site: str | None = None, user: User = Depends(require_page_permission("parking.view")), db: Session = Depends(get_db)):
    service = ParkingService(db); site_id = UUID(site) if site else None
    context = _context(request, db, user, "parking-history"); context.update({"sessions": service.history(site_id), "sites": service.sites(), "selected_site": site or ""})
    return templates.TemplateResponse(request=request, name="parking/history.html", context=context)


@router.get("/parking/sessions/{session_id}", response_class=HTMLResponse)
def session_detail(session_id: UUID, request: Request, user: User = Depends(require_page_permission("parking.view")), db: Session = Depends(get_db)):
    item = ParkingService(db).get_session(session_id)
    if item is None: raise HTTPException(404, "Parking session not found")
    context = _context(request, db, user, "parking-history"); context["session"] = item
    return templates.TemplateResponse(request=request, name="parking/detail.html", context=context)


@router.post("/parking/alerts/{alert_id}/ack")
def acknowledge(alert_id: UUID, request: Request, user: User = Depends(require_page_permission("parking.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    from datetime import datetime, timezone
    alert = db.get(OverstayAlert, alert_id)
    if alert is None: raise HTTPException(404, "Alert not found")
    if alert.status == "OPEN": alert.status = "ACKNOWLEDGED"; alert.acknowledged_at = datetime.now(timezone.utc); alert.acknowledged_by_user_id = user.id
    create_audit_log(db, action="parking.overstay_acknowledge", actor_user_id=user.id, entity_type="overstay_alert", entity_id=str(alert.id), ip_address=request.client.host if request.client else None)
    db.commit(); return RedirectResponse("/parking/on-site", status_code=303)


@router.get("/parking/policy", response_class=HTMLResponse)
def policy_page(request: Request, site: str | None = None, user: User = Depends(require_page_permission("parking.manage")), db: Session = Depends(get_db)):
    service = ParkingService(db); sites = service.sites(); selected = UUID(site) if site else (sites[0].id if sites else None)
    context = _context(request, db, user, "parking-policy"); context.update({"sites": sites, "selected_site": selected, "policy": service.policy(selected) if selected else None, "profiles": service.response_profiles(selected) if selected else (), "cameras": tuple(db.scalars(select(Camera).where(Camera.site_id == selected).order_by(Camera.code)).all()) if selected else (), "simulation": None})
    return templates.TemplateResponse(request=request, name="parking/policy.html", context=context)


@router.post("/parking/policy")
def save_policy(request: Request, site_id: Annotated[UUID, Form()], operating_mode: Annotated[str, Form()], unknown_entry_policy: Annotated[str, Form()], exit_mode: Annotated[str, Form()], exit_blacklist_policy: Annotated[str, Form()], allow_response_profile_id: Annotated[str, Form()] = "", deny_response_profile_id: Annotated[str, Form()] = "", review_response_profile_id: Annotated[str, Form()] = "", user: User = Depends(require_page_permission("parking.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    try: policy = ParkingService(db).save_policy(site_id, operating_mode=operating_mode, unknown_entry=unknown_entry_policy, exit_mode=exit_mode, exit_blacklist=exit_blacklist_policy, allow_profile=allow_response_profile_id, deny_profile=deny_response_profile_id, review_profile=review_response_profile_id)
    except ParkingValidationError as error: raise HTTPException(400, str(error)) from error
    create_audit_log(db, action="parking.policy_update", actor_user_id=user.id, site_id=site_id, entity_type="site_policy", entity_id=str(policy.id), details={"version": policy.policy_version}, ip_address=request.client.host if request.client else None)
    db.commit(); return RedirectResponse(f"/parking/policy?site={site_id}", status_code=303)


@router.post("/parking/policy/simulate", response_class=HTMLResponse)
def simulate_policy(request: Request, site_id: Annotated[UUID, Form()], camera_id: Annotated[UUID, Form()], plate: Annotated[str, Form()], evaluation_time: Annotated[str, Form()] = "", user: User = Depends(require_page_permission("parking.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    from datetime import datetime, timezone
    service = ParkingService(db)
    try:
        at = datetime.fromisoformat(evaluation_time) if evaluation_time.strip() else datetime.now(timezone.utc)
        if at.tzinfo is None: at = at.replace(tzinfo=timezone.utc)
        result = service.simulate(site_id=site_id, camera_id=camera_id, plate=plate, evaluated_at=at)
    except (ValueError, ParkingValidationError) as error:
        raise HTTPException(400, str(error)) from error
    sites = service.sites(); context = _context(request, db, user, "parking-policy")
    context.update({"sites": sites, "selected_site": site_id, "policy": service.policy(site_id), "profiles": service.response_profiles(site_id), "cameras": tuple(db.scalars(select(Camera).where(Camera.site_id == site_id).order_by(Camera.code)).all()), "simulation": result})
    return templates.TemplateResponse(request=request, name="parking/policy.html", context=context)


@router.get("/parking/response-profiles/{profile_id}", response_class=HTMLResponse)
def response_profile_page(profile_id: UUID, request: Request, user: User = Depends(require_page_permission("parking.manage")), db: Session = Depends(get_db)):
    from app.models.parking import DecisionResponseProfile
    profile = db.get(DecisionResponseProfile, profile_id)
    if profile is None: raise HTTPException(404, "Response profile not found")
    context = _context(request, db, user, "parking-policy"); context.update({"profile": profile, "standard_sources": sorted(DECISION_RESPONSE_STANDARD_SOURCES)})
    return templates.TemplateResponse(request=request, name="parking/response_profile.html", context=context)


@router.post("/parking/response-profiles/{profile_id}")
def save_response_profile(profile_id: UUID, request: Request, name: Annotated[str, Form()], display_seconds: Annotated[int, Form()], traffic_state: Annotated[str, Form()], traffic_green_seconds: Annotated[int, Form()], speaker_enabled: Annotated[str | None, Form()] = None, speaker_message: Annotated[str, Form()] = "{plate}", row_1_type: Annotated[str, Form()] = "STANDARD", row_1_value: Annotated[str, Form()] = "PLATE", row_2_type: Annotated[str, Form()] = "CUSTOM", row_2_value: Annotated[str, Form()] = "", row_3_type: Annotated[str, Form()] = "CUSTOM", row_3_value: Annotated[str, Form()] = "", row_4_type: Annotated[str, Form()] = "CUSTOM", row_4_value: Annotated[str, Form()] = "", user: User = Depends(require_page_permission("parking.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    rows = []
    for row_type, value in ((row_1_type,row_1_value),(row_2_type,row_2_value),(row_3_type,row_3_value),(row_4_type,row_4_value)):
        rows.append({"type": row_type, "source": value} if row_type == "STANDARD" else {"type": "CUSTOM", "text": value[:255]})
    try: profile = ParkingService(db).save_response_profile(profile_id, name=name, display_seconds=display_seconds, traffic_state=traffic_state, green_seconds=traffic_green_seconds, speaker_enabled=speaker_enabled == "true", speaker_message=speaker_message, rows=rows)
    except ParkingValidationError as error: raise HTTPException(400, str(error)) from error
    create_audit_log(db, action="parking.response_profile_update", actor_user_id=user.id, site_id=profile.site_id, entity_type="decision_response_profile", entity_id=str(profile.id), details={"name": profile.name, "kind": profile.response_kind}, ip_address=request.client.host if request.client else None)
    db.commit(); return RedirectResponse(f"/parking/policy?site={profile.site_id}", status_code=303)


@router.get("/access-profiles", response_class=HTMLResponse)
def profiles(request: Request, user: User = Depends(require_page_permission("parking.manage")), db: Session = Depends(get_db)):
    service = ParkingService(db); context = _context(request, db, user, "access-profiles"); context.update({"profiles": service.profiles(), "sites": service.sites(), "site_exit_rules": service.site_exit_rules()})
    return templates.TemplateResponse(request=request, name="parking/profiles.html", context=context)


@router.post("/access-profiles")
def save_profile(request: Request, site_id: Annotated[UUID, Form()], name: Annotated[str, Form()], description: Annotated[str, Form()] = "", default_max_stay_minutes: Annotated[str, Form()] = "", stay_limit_mode: Annotated[str, Form()] = "", maximum_stay_value: Annotated[str, Form()] = "", maximum_stay_unit: Annotated[str, Form()] = "MINUTES", overstay_monitoring_enabled: Annotated[str | None, Form()] = None, enabled: Annotated[str | None, Form()] = None, normal_exit_policy: Annotated[str, Form()] = "USE_SITE_POLICY", expired_exit_policy: Annotated[str, Form()] = "USE_SITE_POLICY", overstay_exit_policy: Annotated[str, Form()] = "REVIEW", blacklist_exit_policy: Annotated[str, Form()] = "DENY", overstay_exit_grace_minutes: Annotated[str, Form()] = "0", user: User = Depends(require_page_permission("parking.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    try: profile = ParkingService(db).save_profile(profile_id=None, site_id=site_id, name=name, description=description, max_stay=_maximum_stay_minutes(default_max_stay_minutes, stay_limit_mode, maximum_stay_value, maximum_stay_unit), monitoring=overstay_monitoring_enabled == "true", enabled=enabled == "true", normal_exit_policy=normal_exit_policy, expired_exit_policy=expired_exit_policy, overstay_exit_policy=overstay_exit_policy, blacklist_exit_policy=blacklist_exit_policy, overstay_exit_grace_minutes=overstay_exit_grace_minutes)
    except ParkingValidationError as error: raise HTTPException(400, str(error)) from error
    create_audit_log(db, action="parking.access_profile_create", actor_user_id=user.id, site_id=site_id, entity_type="access_profile", entity_id=str(profile.id), details={"name": profile.name}, ip_address=request.client.host if request.client else None)
    db.commit(); return RedirectResponse("/access-profiles", status_code=303)


@router.get("/access-profiles/{profile_id}", response_class=HTMLResponse)
def edit_profile_page(profile_id: UUID, request: Request, user: User = Depends(require_page_permission("parking.manage")), db: Session = Depends(get_db)):
    from app.models.parking import AccessProfile
    profile = db.get(AccessProfile, profile_id)
    if profile is None: raise HTTPException(404, "Access profile not found")
    service = ParkingService(db); context = _context(request, db, user, "access-profiles"); context.update({"profile": profile, "sites": service.sites(), "site_exit_rules": service.site_exit_rules()})
    return templates.TemplateResponse(request=request, name="parking/profile_edit.html", context=context)


@router.post("/access-profiles/{profile_id}")
def update_profile(profile_id: UUID, request: Request, site_id: Annotated[UUID, Form()], name: Annotated[str, Form()], description: Annotated[str, Form()] = "", default_max_stay_minutes: Annotated[str, Form()] = "", stay_limit_mode: Annotated[str, Form()] = "", maximum_stay_value: Annotated[str, Form()] = "", maximum_stay_unit: Annotated[str, Form()] = "MINUTES", overstay_monitoring_enabled: Annotated[str | None, Form()] = None, enabled: Annotated[str | None, Form()] = None, normal_exit_policy: Annotated[str, Form()] = "USE_SITE_POLICY", expired_exit_policy: Annotated[str, Form()] = "USE_SITE_POLICY", overstay_exit_policy: Annotated[str, Form()] = "REVIEW", blacklist_exit_policy: Annotated[str, Form()] = "DENY", overstay_exit_grace_minutes: Annotated[str, Form()] = "0", user: User = Depends(require_page_permission("parking.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    try: profile = ParkingService(db).save_profile(profile_id=profile_id, site_id=site_id, name=name, description=description, max_stay=_maximum_stay_minutes(default_max_stay_minutes, stay_limit_mode, maximum_stay_value, maximum_stay_unit), monitoring=overstay_monitoring_enabled == "true", enabled=enabled == "true", normal_exit_policy=normal_exit_policy, expired_exit_policy=expired_exit_policy, overstay_exit_policy=overstay_exit_policy, blacklist_exit_policy=blacklist_exit_policy, overstay_exit_grace_minutes=overstay_exit_grace_minutes)
    except ParkingValidationError as error: raise HTTPException(400, str(error)) from error
    create_audit_log(db, action="parking.access_profile_update", actor_user_id=user.id, site_id=profile.site_id, entity_type="access_profile", entity_id=str(profile.id), details={"name": profile.name, "enabled": profile.enabled}, ip_address=request.client.host if request.client else None)
    db.commit(); return RedirectResponse("/access-profiles", status_code=303)


@router.get("/parking/reviews", response_class=HTMLResponse)
def review_queue(request: Request, site: str = "", status: str = "", item_type: str = "", plate: str = "", page: int = 1,
                 user: User = Depends(require_page_permission("parking.view")), db: Session = Depends(get_db)):
    service = ParkingService(db)
    try: site_id = UUID(site) if site else None
    except ValueError as error: raise HTTPException(400, "Invalid site filter") from error
    result = service.reviews(site_id=site_id, status=status, item_type=item_type, plate=plate, page=max(1, page))
    context = _context(request, db, user, "parking-reviews")
    context.update({"result": result, "sites": service.sites(), "filters": {"site": site, "status": status, "item_type": item_type, "plate": plate}, "can_manage": user_has_permission(user, "parking.manage")})
    return templates.TemplateResponse(request=request, name="parking/reviews.html", context=context)


@router.post("/parking/reviews/{item_id}/ack")
def acknowledge_review(item_id: UUID, request: Request, user: User = Depends(require_page_permission("parking.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    item = ParkingReviewService(db).acknowledge(item_id, user.id)
    if item is None: raise HTTPException(404, "Review item not found")
    create_audit_log(db, action="parking.review_acknowledge", actor_user_id=user.id, site_id=item.site_id, entity_type="parking_review_item", entity_id=str(item.id), ip_address=request.client.host if request.client else None)
    db.commit(); return RedirectResponse("/parking/reviews", status_code=303)


@router.post("/parking/reviews/{item_id}/resolve")
def resolve_review(item_id: UUID, request: Request, resolution_note: Annotated[str, Form()] = "", user: User = Depends(require_page_permission("parking.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    item = ParkingReviewService(db).resolve(item_id, user.id, resolution_note)
    if item is None: raise HTTPException(404, "Review item not found")
    create_audit_log(db, action="parking.review_resolve", actor_user_id=user.id, site_id=item.site_id, entity_type="parking_review_item", entity_id=str(item.id), details={"note": item.resolution_note}, ip_address=request.client.host if request.client else None)
    db.commit(); return RedirectResponse("/parking/reviews", status_code=303)


def _maximum_stay_minutes(legacy_minutes: str, mode: str, value: str, unit: str) -> str:
    if not mode:
        return legacy_minutes
    if mode == "UNLIMITED":
        return ""
    if mode != "LIMITED" or unit not in {"MINUTES", "HOURS", "DAYS"}:
        raise ParkingValidationError("Select a valid maximum-stay option.")
    try:
        amount = int(value)
    except ValueError as error:
        raise ParkingValidationError("Enter a whole-number maximum stay.") from error
    if amount <= 0:
        raise ParkingValidationError("Maximum stay must be greater than zero.")
    return str(amount * {"MINUTES": 1, "HOURS": 60, "DAYS": 1440}[unit])


__all__ = ["router"]
