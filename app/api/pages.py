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
    user_has_permission,
)
from app.models.user import User
from app.services.dashboard_service import get_dashboard_summary
from app.services.mqtt_service import mqtt_heartbeat_service
from app.services.media_service import MediaPlaybackService
from app.services.recognition_service import RecognitionService
from app.services.parking_service import ParkingService
from app.services.shell_service import build_navigation, role_label
from app.services.barrier_query_service import dashboard_barrier_exceptions
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
    can_view_events = user_has_permission(user, "events.view")
    can_view_parking = user_has_permission(user, "parking.view")
    can_view_media = user_has_permission(user, "media.view")
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
            "mqtt_runtime": mqtt_heartbeat_service.runtime_status(),
            "can_view_events": can_view_events,
            "can_manage_access_list": user_has_permission(user, "access_list.manage"),
            "can_view_cameras": user_has_permission(user, "cameras.view"),
            "can_view_parking": can_view_parking,
            "can_view_media": can_view_media,
            "live_media_sources": (
                MediaPlaybackService(db).live_sources() if can_view_media else ()
            ),
            "parking_summary": ParkingService(db).summary() if can_view_parking else None,
            "overstay_alerts": ParkingService(db).alerts() if can_view_parking else (),
            "barrier_exceptions": dashboard_barrier_exceptions(db) if can_view_parking else (),
            "latest_recognition": (
                RecognitionService(db).latest() if can_view_events else None
            ),
            "live_cursor": (
                RecognitionService(db).current_cursor() if can_view_events else ""
            ),
            "live_poll_seconds": settings.live_poll_seconds,
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
