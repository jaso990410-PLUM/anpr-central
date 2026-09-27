from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.db.dependencies import get_db
from app.dependencies.auth import (
    require_page_permission,
    user_has_permission,
)
from app.dependencies.csrf import require_session_csrf
from app.models.user import User
from app.services.audit_service import create_audit_log
from app.services.dashboard_service import get_dashboard_summary
from app.services.shell_service import build_navigation, role_label
from app.services.user_service import UserService, UserValidationError
from app.web import templates


router = APIRouter(prefix="/users")


def _shell_context(
    request: Request,
    db: Session,
    user: User,
    raw_session_token: str,
) -> dict[str, object]:
    return {
        "request": request,
        "user": user,
        "csrf_token": generate_csrf_token(raw_session_token),
        "summary": get_dashboard_summary(db),
        "active_page": "users",
        "navigation": build_navigation(user),
        "user_role_label": role_label(user),
    }


def _render_create_form(
    request: Request,
    db: Session,
    user: User,
    raw_session_token: str,
    *,
    values: dict[str, object] | None = None,
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    context = _shell_context(request, db, user, raw_session_token)
    context.update(
        {
            "roles": UserService(db).list_roles(),
            "values": values or {"is_active": True, "role_ids": []},
            "error": error,
        }
    )
    return templates.TemplateResponse(
        request=request,
        name="users/new.html",
        context=context,
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def user_list(
    request: Request,
    q: Annotated[str | None, Query(max_length=255)] = None,
    user: User = Depends(require_page_permission("users.view")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    raw_session_token = request.cookies[settings.session_cookie_name]
    context = _shell_context(request, db, user, raw_session_token)
    context.update(
        {
            "users": UserService(db).list_users(q),
            "search_query": (q or "").strip(),
            "can_manage_users": user_has_permission(user, "users.manage"),
        }
    )
    return templates.TemplateResponse(
        request=request,
        name="users/list.html",
        context=context,
    )


@router.get("/new", response_class=HTMLResponse)
def new_user_page(
    request: Request,
    user: User = Depends(require_page_permission("users.manage")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    raw_session_token = request.cookies[settings.session_cookie_name]
    return _render_create_form(request, db, user, raw_session_token)


@router.post("/new", response_class=HTMLResponse)
def create_user(
    request: Request,
    username: Annotated[str, Form()],
    full_name: Annotated[str, Form()],
    email: Annotated[str | None, Form()] = None,
    role_ids: Annotated[list[str] | None, Form()] = None,
    temporary_password: Annotated[str, Form()] = "",
    confirm_password: Annotated[str, Form()] = "",
    is_active: Annotated[str | None, Form()] = None,
    user: User = Depends(require_page_permission("users.manage")),
    raw_session_token: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> Response:
    selected_role_ids = role_ids or []
    active = is_active == "true"
    safe_values: dict[str, object] = {
        "username": username,
        "full_name": full_name,
        "email": email or "",
        "role_ids": selected_role_ids,
        "is_active": active,
    }
    service = UserService(db)
    try:
        created_user = service.create_user(
            username=username,
            full_name=full_name,
            email=email,
            role_ids=selected_role_ids,
            temporary_password=temporary_password,
            confirm_password=confirm_password,
            is_active=active,
        )
    except UserValidationError as error:
        return _render_create_form(
            request,
            db,
            user,
            raw_session_token,
            values=safe_values,
            error=str(error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    create_audit_log(
        db,
        action="user.create",
        actor_user_id=user.id,
        entity_type="user",
        entity_id=str(created_user.id),
        details={
            "created_user_id": str(created_user.id),
            "username": created_user.username,
            "role_codes": sorted(role.code for role in created_user.roles),
            "is_active": created_user.is_active,
        },
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)
