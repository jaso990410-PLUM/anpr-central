from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.db.dependencies import get_db
from app.dependencies.auth import require_page_permission, user_has_permission
from app.dependencies.csrf import require_session_csrf
from app.models.user import User
from app.services.audit_service import create_audit_log
from app.services.dashboard_service import get_dashboard_summary
from app.services.peripheral_preset_service import PeripheralPresetService, PeripheralPresetValidationError, STANDARD_SOURCES
from app.services.shell_service import build_navigation, role_label
from app.web import templates


router = APIRouter(prefix="/peripheral-presets")


def _context(request: Request, db: Session, user: User, **extra):
    context = {"request": request, "user": user, "csrf_token": generate_csrf_token(request.cookies[settings.session_cookie_name]), "summary": get_dashboard_summary(db), "active_page": "peripheral-presets", "navigation": build_navigation(user), "user_role_label": role_label(user), "can_manage": user_has_permission(user, "control_board.manage"), "standard_sources": sorted(STANDARD_SOURCES)}
    context.update(extra)
    return context


@router.get("", response_class=HTMLResponse)
def list_page(request: Request, user: User = Depends(require_page_permission("control_board.view")), db: Session = Depends(get_db)):
    return templates.TemplateResponse(request=request, name="peripheral_presets/list.html", context=_context(request, db, user, presets=PeripheralPresetService(db).list()))


@router.get("/new", response_class=HTMLResponse)
def new_page(request: Request, user: User = Depends(require_page_permission("control_board.manage")), db: Session = Depends(get_db)):
    return templates.TemplateResponse(request=request, name="peripheral_presets/new.html", context=_context(request, db, user, error=None))


@router.post("/new")
async def create(request: Request, user: User = Depends(require_page_permission("control_board.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    form = dict(await request.form())
    try:
        preset = PeripheralPresetService(db).create(name=form.get("name"), description=form.get("description"), enabled=form.get("enabled") == "true")
        create_audit_log(db, action="peripheral_preset.create", actor_user_id=user.id, entity_type="peripheral_preset", entity_id=str(preset.id), details={"name": preset.name, "enabled": preset.enabled}, ip_address=request.client.host if request.client else None)
        db.commit()
    except PeripheralPresetValidationError as error:
        db.rollback()
        return templates.TemplateResponse(request=request, name="peripheral_presets/new.html", context=_context(request, db, user, error=str(error)), status_code=400)
    return RedirectResponse(f"/peripheral-presets/{preset.id}", status_code=303)


@router.get("/{preset_id}", response_class=HTMLResponse)
def detail(request: Request, preset_id: UUID, user: User = Depends(require_page_permission("control_board.view")), db: Session = Depends(get_db)):
    preset = PeripheralPresetService(db).get(preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="Preset not found")
    return templates.TemplateResponse(request=request, name="peripheral_presets/detail.html", context=_context(request, db, user, preset=preset, error=None))


@router.post("/{preset_id}")
async def update(request: Request, preset_id: UUID, user: User = Depends(require_page_permission("control_board.manage")), _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)):
    service = PeripheralPresetService(db)
    preset = service.get(preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="Preset not found")
    form = dict(await request.form())
    try:
        service.update(preset, form)
        create_audit_log(db, action="peripheral_preset.update", actor_user_id=user.id, entity_type="peripheral_preset", entity_id=str(preset.id), details={"name": preset.name, "enabled": preset.enabled, "busy_period_count": len(preset.busy_periods)}, ip_address=request.client.host if request.client else None)
        db.commit()
    except (PeripheralPresetValidationError, UnicodeError) as error:
        db.rollback()
        preset = service.get(preset_id)
        return templates.TemplateResponse(request=request, name="peripheral_presets/detail.html", context=_context(request, db, user, preset=preset, error=str(error)), status_code=400)
    return RedirectResponse(f"/peripheral-presets/{preset.id}?saved=1", status_code=303)
