from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.cookies import (
    delete_login_csrf_cookie,
    delete_session_cookie,
    set_login_csrf_cookie,
    set_session_cookie,
)
from app.core.csrf import generate_csrf_token, verify_csrf_token
from app.core.navigation import safe_local_redirect
from app.core.security import generate_session_token
from app.db.dependencies import get_db
from app.dependencies.auth import (
    get_current_user,
    require_authenticated_page_user,
)
from app.dependencies.csrf import require_session_csrf
from app.models.user import User
from app.services.audit_service import create_audit_log
from app.services.auth_service import (
    INVALID_CREDENTIALS_MESSAGE,
    AuthenticationError,
    authenticate_user,
)
from app.services.session_service import create_session, revoke_session
from app.web import templates


router = APIRouter()


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


def _render_login(
    request: Request,
    *,
    next_destination: str,
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    csrf_nonce = request.cookies.get(settings.csrf_cookie_name)
    if not csrf_nonce:
        csrf_nonce = generate_session_token()

    response = templates.TemplateResponse(
        request=request,
        name="auth/login.html",
        context={
            "error": error,
            "next_destination": next_destination,
            "csrf_token": generate_csrf_token(csrf_nonce),
        },
        status_code=status_code,
    )
    set_login_csrf_cookie(response, csrf_nonce)
    return response


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next: str | None = None,
    current_user: User | None = Depends(get_current_user),
) -> Response:
    if current_user is not None:
        return RedirectResponse(
            safe_local_redirect(next),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    return _render_login(
        request,
        next_destination=safe_local_redirect(next),
    )


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: Annotated[str, Form(max_length=80)],
    password: Annotated[str, Form(max_length=1024)],
    csrf_token: Annotated[str | None, Form()] = None,
    next: Annotated[str, Form()] = "/dashboard",
    db: Session = Depends(get_db),
) -> Response:
    destination = safe_local_redirect(next)
    csrf_nonce = request.cookies.get(settings.csrf_cookie_name)
    if (
        not csrf_nonce
        or not csrf_token
        or not verify_csrf_token(csrf_token, csrf_nonce)
    ):
        return _render_login(
            request,
            next_destination=destination,
            error="The form expired. Please try again.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        user = authenticate_user(db, username, password)
    except AuthenticationError:
        create_audit_log(
            db,
            action="login.failed",
            details={"username": username},
            ip_address=_client_ip(request),
        )
        db.commit()
        return _render_login(
            request,
            next_destination=destination,
            error=INVALID_CREDENTIALS_MESSAGE,
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    raw_session_token, _ = create_session(
        db,
        user,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    create_audit_log(
        db,
        action="login.succeeded",
        actor_user_id=user.id,
        ip_address=_client_ip(request),
    )
    db.commit()

    if user.must_change_password:
        destination = "/account/change-password"

    response = RedirectResponse(
        destination,
        status_code=status.HTTP_303_SEE_OTHER,
    )
    set_session_cookie(response, raw_session_token)
    delete_login_csrf_cookie(response)
    return response


@router.post("/logout")
def logout(
    request: Request,
    raw_session_token: str = Depends(require_session_csrf),
    user: User = Depends(require_authenticated_page_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    revoke_session(db, raw_session_token)
    create_audit_log(
        db,
        action="logout",
        actor_user_id=user.id,
        ip_address=_client_ip(request),
    )
    db.commit()

    response = RedirectResponse(
        "/login",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    delete_session_cookie(response)
    return response
