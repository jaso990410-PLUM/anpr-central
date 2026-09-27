from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.cookies import set_session_cookie
from app.core.csrf import generate_csrf_token
from app.db.dependencies import get_db
from app.dependencies.auth import require_authenticated_page_user
from app.dependencies.csrf import require_session_csrf
from app.models.user import User
from app.services.account_service import PasswordChangeError, change_password
from app.services.audit_service import create_audit_log
from app.services.dashboard_service import get_dashboard_summary
from app.services.session_service import create_session, revoke_all_user_sessions
from app.services.shell_service import build_navigation, role_label
from app.web import templates


router = APIRouter(prefix="/account")


def _render_change_password(
    request: Request,
    db: Session,
    user: User,
    raw_session_token: str,
    *,
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="account/change_password.html",
        context={
            "user": user,
            "summary": get_dashboard_summary(db),
            "navigation": build_navigation(
                user,
                password_change_only=user.must_change_password,
            ),
            "user_role_label": role_label(user),
            "active_page": "account-security",
            "csrf_token": generate_csrf_token(raw_session_token),
            "password_change_required": user.must_change_password,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/change-password", response_class=HTMLResponse)
def change_password_page(
    request: Request,
    user: User = Depends(require_authenticated_page_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    raw_session_token = request.cookies[settings.session_cookie_name]
    return _render_change_password(
        request,
        db,
        user,
        raw_session_token,
    )


@router.post("/change-password", response_class=HTMLResponse)
def update_password(
    request: Request,
    current_password: Annotated[str, Form(max_length=1024)],
    new_password: Annotated[str, Form(max_length=1024)],
    confirm_password: Annotated[str, Form(max_length=1024)],
    raw_session_token: str = Depends(require_session_csrf),
    user: User = Depends(require_authenticated_page_user),
    db: Session = Depends(get_db),
) -> Response:
    try:
        change_password(
            db,
            user,
            current_password=current_password,
            new_password=new_password,
            confirm_password=confirm_password,
        )
    except PasswordChangeError as error:
        return _render_change_password(
            request,
            db,
            user,
            raw_session_token,
            error=str(error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    revoked_session_count = revoke_all_user_sessions(db, user.id)
    new_raw_session_token, _ = create_session(
        db,
        user,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    create_audit_log(
        db,
        action="account.password_change",
        actor_user_id=user.id,
        details={"sessions_revoked": revoked_session_count},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()

    response = RedirectResponse(
        "/dashboard",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    set_session_cookie(response, new_raw_session_token)
    return response
