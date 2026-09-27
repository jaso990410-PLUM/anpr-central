from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.db.dependencies import get_db
from app.dependencies.auth import require_page_permission
from app.models.user import User
from app.services.audit_service import AuditService
from app.services.dashboard_service import get_dashboard_summary
from app.services.shell_service import build_navigation, role_label
from app.web import templates


router = APIRouter()


@router.get("/audit", response_class=HTMLResponse)
def audit_log_page(
    request: Request,
    action: Annotated[str | None, Query(max_length=100)] = None,
    actor: Annotated[str | None, Query(max_length=255)] = None,
    date_from: date | None = None,
    date_to: date | None = None,
    user: User = Depends(require_page_permission("audit.view")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Start date must not be after end date",
        )

    service = AuditService(db)
    raw_session_token = request.cookies[settings.session_cookie_name]
    return templates.TemplateResponse(
        request=request,
        name="audit/list.html",
        context={
            "user": user,
            "csrf_token": generate_csrf_token(raw_session_token),
            "summary": get_dashboard_summary(db),
            "active_page": "audit",
            "navigation": build_navigation(user),
            "user_role_label": role_label(user),
            "entries": service.list_entries(
                action=action,
                actor=actor,
                date_from=date_from,
                date_to=date_to,
            ),
            "actions": service.list_actions(),
            "actors": service.list_actors(),
            "filters": {
                "action": (action or "").strip(),
                "actor": (actor or "").strip(),
                "date_from": date_from.isoformat() if date_from else "",
                "date_to": date_to.isoformat() if date_to else "",
            },
        },
    )
