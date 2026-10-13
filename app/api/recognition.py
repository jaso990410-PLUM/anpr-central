from datetime import date
from typing import Annotated, Literal
from uuid import UUID
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BeforeValidator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.db.dependencies import get_db
from app.dependencies.auth import require_page_permission, require_permission, user_has_permission
from app.models.camera import Camera
from app.models.recognition_event import RecognitionEvent
from app.models.user import User
from app.models.media import EventMediaReference
from app.services.dashboard_service import get_dashboard_summary
from app.services.recognition_media_service import (
    RecognitionMediaError,
    RecognitionMediaService,
)
from app.services.recognition_service import LiveCursorError, RecognitionService
from app.services.shell_service import build_navigation, role_label
from app.web import templates


router = APIRouter()


def _blank_query_value_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


OptionalCameraQuery = Annotated[
    UUID | None, BeforeValidator(_blank_query_value_to_none), Query()
]
OptionalSourceQuery = Annotated[
    Literal["LIVE", "OFFLINE_RECOVERY"] | None,
    BeforeValidator(_blank_query_value_to_none),
    Query(),
]
OptionalDateQuery = Annotated[
    date | None, BeforeValidator(_blank_query_value_to_none), Query()
]


@router.get("/recognition", response_class=HTMLResponse)
def recognition_history(
    request: Request,
    plate: Annotated[str | None, Query(max_length=32)] = None,
    camera: OptionalCameraQuery = None,
    direction: Annotated[str | None, Query(max_length=20)] = None,
    source: OptionalSourceQuery = None,
    date_from: OptionalDateQuery = None,
    date_to: OptionalDateQuery = None,
    page: Annotated[int, Query(ge=1)] = 1,
    user: User = Depends(require_page_permission("events.view")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Start date must not be after end date",
        )
    service = RecognitionService(db)
    result = service.history(
        plate=plate,
        camera_id=camera,
        direction=direction,
        source=source,
        date_from=date_from,
        date_to=date_to,
        page=page,
    )
    event_ids = [UUID(item["id"]) for item in result.items]
    media_states = {str(item.recognition_event_id): item.status for item in db.scalars(select(EventMediaReference).where(EventMediaReference.recognition_event_id.in_(event_ids))).all()} if event_ids else {}
    for item in result.items:
        item["media_status"] = media_states.get(item["id"], "NOT_CONFIGURED")
    raw_session_token = request.cookies[settings.session_cookie_name]
    query_values = {
        "plate": (plate or "").strip(),
        "camera": str(camera) if camera else "",
        "direction": (direction or "").strip().upper(),
        "source": source or "",
        "date_from": date_from.isoformat() if date_from else "",
        "date_to": date_to.isoformat() if date_to else "",
    }
    def page_url(target: int) -> str:
        return "/recognition?" + urlencode(
            {**query_values, "page": target}
        )
    return templates.TemplateResponse(
        request=request,
        name="recognition/list.html",
        context={
            "user": user,
            "csrf_token": generate_csrf_token(raw_session_token),
            "summary": get_dashboard_summary(db),
            "active_page": "recognition",
            "navigation": build_navigation(user),
            "user_role_label": role_label(user),
            "result": result,
            "cameras": db.scalars(select(Camera).order_by(Camera.code)).all(),
            "filters": query_values,
            "previous_url": page_url(result.page - 1) if result.page > 1 else None,
            "next_url": page_url(result.page + 1) if result.page < result.page_count else None,
            "live_cursor": service.current_cursor(source=source) or "",
            "live_poll_seconds": settings.live_poll_seconds,
            "can_manage_access_list": user_has_permission(user, "access_list.manage"),
            "can_view_parking": user_has_permission(user, "parking.view"),
            "can_view_operations": user_has_permission(user, "operations.view"),
            "can_correct_plate": user_has_permission(user, "recognition.correct"),
            "can_view_media": user_has_permission(user, "media.view"),
        },
    )


@router.get("/recognition/{event_id}/image/{kind}")
def recognition_image(
    event_id: UUID,
    kind: Literal["scene", "plate"],
    _user: User = Depends(require_permission("events.view")),
    db: Session = Depends(get_db),
) -> FileResponse:
    event = db.get(RecognitionEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Recognition event not found")
    reference = (
        event.scene_image_path if kind == "scene" else event.plate_image_path
    )
    if not reference:
        raise HTTPException(status_code=404, detail="Recognition image not found")
    try:
        path = RecognitionMediaService().resolve(reference)
    except RecognitionMediaError as error:
        raise HTTPException(status_code=404, detail="Recognition image not found") from error
    media_type = {
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".bmp": "image/bmp",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type)


@router.get("/api/recognitions/live")
def live_recognitions(
    after: Annotated[str | None, Query(max_length=300)] = None,
    source: OptionalSourceQuery = None,
    _user: User = Depends(require_permission("events.view")),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    service = RecognitionService(db)
    try:
        events, cursor = service.changed_after(after, limit=50, source=source)
    except LiveCursorError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "events": events,
        "latest": service.latest(),
        "access_updates": service.recent_access(limit=100),
        "cursor": cursor,
    }
