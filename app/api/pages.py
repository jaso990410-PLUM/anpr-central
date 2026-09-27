from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.db.dependencies import get_db
from app.dependencies.auth import (
    get_current_user,
    require_authenticated_user_page,
)
from app.models.user import User
from app.services.dashboard_service import get_dashboard_summary
from app.services.shell_service import build_navigation, role_label
from app.web import templates


router = APIRouter()


@router.get("/")
def root(
    current_user: User | None = Depends(get_current_user),
) -> RedirectResponse:
    destination = "/dashboard" if current_user is not None else "/login"
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: User = Depends(require_authenticated_user_page),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    raw_session_token = request.cookies[settings.session_cookie_name]
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": user,
            "csrf_token": generate_csrf_token(raw_session_token),
            "summary": get_dashboard_summary(db),
            "active_page": "dashboard",
            "navigation": build_navigation(user),
            "user_role_label": role_label(user),
        },
    )


def login_redirect_for(request: Request) -> RedirectResponse:
    destination = request.url.path
    if request.url.query:
        destination = f"{destination}?{request.url.query}"

    return RedirectResponse(
        f"/login?{urlencode({'next': destination})}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
