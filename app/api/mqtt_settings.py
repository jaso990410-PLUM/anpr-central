from datetime import datetime, timezone
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.db.dependencies import get_db
from app.dependencies.auth import require_page_permission
from app.dependencies.csrf import require_session_csrf
from app.models.camera import Camera
from app.models.user import User
from app.services.audit_service import create_audit_log
from app.services.camera_service import ONLINE
from app.services.dashboard_service import get_dashboard_summary
from app.services.mqtt_config_service import (
    EffectiveMqttConfig,
    MqttConfigInput,
    MqttConfigService,
    MqttConfigValidationError,
)
from app.services.mqtt_connection_tester import mqtt_connection_tester
from app.services.mqtt_service import mqtt_heartbeat_service
from app.services.shell_service import build_navigation, role_label
from app.web import templates


router = APIRouter(prefix="/settings/mqtt")


def _input(
    *,
    mqtt_enabled: str | None,
    host: str,
    port: str,
    username: str | None,
    client_id: str,
    heartbeat_timeout_seconds: str,
) -> MqttConfigInput:
    try:
        parsed_port = int(port)
    except ValueError as error:
        raise MqttConfigValidationError("Broker port must be a whole number.") from error
    try:
        parsed_timeout = int(heartbeat_timeout_seconds)
    except ValueError as error:
        raise MqttConfigValidationError(
            "Camera heartbeat timeout must be a whole number."
        ) from error
    return MqttConfigInput(
        is_enabled=mqtt_enabled == "true",
        host=host,
        port=parsed_port,
        username=username,
        client_id=client_id,
        heartbeat_timeout_seconds=parsed_timeout,
    )


def _values(configuration: EffectiveMqttConfig) -> dict[str, object]:
    return {
        "is_enabled": configuration.is_enabled,
        "host": configuration.host,
        "port": configuration.port,
        "username": configuration.username or "",
        "client_id": configuration.client_id,
        "heartbeat_timeout_seconds": configuration.heartbeat_timeout_seconds,
        "source": configuration.source,
    }


def _submitted_values(values: MqttConfigInput) -> dict[str, object]:
    return {
        "is_enabled": values.is_enabled,
        "host": values.host,
        "port": values.port,
        "username": values.username or "",
        "client_id": values.client_id,
        "heartbeat_timeout_seconds": values.heartbeat_timeout_seconds,
        "source": "candidate",
    }


def _display_time(value: datetime | None, timezone_name: str) -> str:
    if value is None:
        return "Never"
    try:
        target_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        target_timezone = timezone.utc
    utc_value = (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
    return utc_value.astimezone(target_timezone).strftime("%Y-%m-%d %H:%M:%S")


def _render(
    request: Request,
    db: Session,
    user: User,
    raw_session_token: str,
    *,
    values: dict[str, object],
    error: str | None = None,
    test_result: object | None = None,
    saved: bool = False,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    summary = get_dashboard_summary(db)
    runtime = mqtt_heartbeat_service.runtime_status()
    timezone_name = summary.site.timezone if summary.site else "Asia/Kuala_Lumpur"
    online_cameras = db.scalar(
        select(func.count()).select_from(Camera).where(
            Camera.connection_state == ONLINE
        )
    ) or 0
    return templates.TemplateResponse(
        request=request,
        name="settings/mqtt.html",
        context={
            "user": user,
            "csrf_token": generate_csrf_token(raw_session_token),
            "summary": summary,
            "active_page": "mqtt-settings",
            "navigation": build_navigation(user),
            "user_role_label": role_label(user),
            "values": values,
            "error": error,
            "test_result": test_result,
            "saved": saved,
            "runtime": runtime,
            "last_connected_display": _display_time(
                runtime.last_connected_at, timezone_name
            ),
            "online_cameras": online_cameras,
        },
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def mqtt_settings_page(
    request: Request,
    saved: bool = False,
    user: User = Depends(require_page_permission("settings.manage")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    raw_session_token = request.cookies[settings.session_cookie_name]
    configuration = MqttConfigService(db).effective()
    return _render(
        request,
        db,
        user,
        raw_session_token,
        values=_values(configuration),
        saved=saved,
    )


@router.post("")
def save_mqtt_settings(
    request: Request,
    host: Annotated[str, Form()],
    port: Annotated[str, Form()],
    client_id: Annotated[str, Form()],
    heartbeat_timeout_seconds: Annotated[str, Form()],
    username: Annotated[str | None, Form()] = None,
    password: Annotated[str, Form()] = "",
    mqtt_enabled: Annotated[str | None, Form()] = None,
    user: User = Depends(require_page_permission("settings.manage")),
    raw_session_token: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> Response:
    try:
        submitted = _input(
            mqtt_enabled=mqtt_enabled,
            host=host,
            port=port,
            username=username,
            client_id=client_id,
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        )
        configuration, changes = MqttConfigService(db).save(
            submitted, submitted_password=password
        )
    except MqttConfigValidationError as error:
        fallback = MqttConfigInput(
            is_enabled=mqtt_enabled == "true",
            host=host,
            port=int(port) if port.isdigit() else 0,
            username=username,
            client_id=client_id,
            heartbeat_timeout_seconds=(
                int(heartbeat_timeout_seconds)
                if heartbeat_timeout_seconds.isdigit()
                else 0
            ),
        )
        return _render(
            request,
            db,
            user,
            raw_session_token,
            values=_submitted_values(fallback),
            error=str(error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    create_audit_log(
        db,
        action="mqtt.configuration_update",
        actor_user_id=user.id,
        entity_type="system_setting",
        entity_id="mqtt",
        details=changes,
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    mqtt_heartbeat_service.reconfigure(configuration)
    return RedirectResponse(
        "/settings/mqtt?saved=true",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/test", response_class=HTMLResponse)
def test_mqtt_settings(
    request: Request,
    host: Annotated[str, Form()],
    port: Annotated[str, Form()],
    client_id: Annotated[str, Form()],
    heartbeat_timeout_seconds: Annotated[str, Form()],
    username: Annotated[str | None, Form()] = None,
    password: Annotated[str, Form()] = "",
    mqtt_enabled: Annotated[str | None, Form()] = None,
    user: User = Depends(require_page_permission("settings.manage")),
    raw_session_token: str = Depends(require_session_csrf),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        submitted = _input(
            mqtt_enabled=mqtt_enabled,
            host=host,
            port=port,
            username=username,
            client_id=client_id,
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        )
        candidate = MqttConfigService(db).candidate(
            submitted, submitted_password=password
        )
    except MqttConfigValidationError as error:
        return _render(
            request,
            db,
            user,
            raw_session_token,
            values={
                "is_enabled": mqtt_enabled == "true",
                "host": host,
                "port": port,
                "username": username or "",
                "client_id": client_id,
                "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
                "source": "candidate",
            },
            error=str(error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    result = mqtt_connection_tester.test(candidate)
    create_audit_log(
        db,
        action="mqtt.connection_test",
        actor_user_id=user.id,
        entity_type="system_setting",
        entity_id="mqtt",
        details={
            "host": candidate.host,
            "port": candidate.port,
            "success": result.success,
        },
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return _render(
        request,
        db,
        user,
        raw_session_token,
        values=_submitted_values(submitted),
        test_result=result,
    )
