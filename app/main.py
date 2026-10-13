from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from app.api.account import router as account_router
from app.api.access_list import router as access_list_router
from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.barriers import router as barriers_router
from app.api.cameras import router as cameras_router
from app.api.control_boards import router as control_boards_router
from app.api.mqtt_settings import router as mqtt_settings_router
from app.api.media import router as media_router
from app.api.operations import router as operations_router
from app.api.live_status import router as live_status_router
from app.api.recognition import router as recognition_router
from app.api.residential import router as residential_router
from app.api.pages import login_redirect_for, router as pages_router
from app.api.peripheral_presets import router as peripheral_presets_router
from app.api.parking import router as parking_router
from app.api.users import router as users_router
from app.core.config import settings
from app.db.session import SessionLocal, engine
from app.dependencies.auth import (
    PageAuthenticationRequired,
    PasswordChangeRequired,
)
from app.services.mqtt_service import mqtt_heartbeat_service
from app.services.mqtt_config_service import MqttConfigService
from app.services.stay_monitor_service import stay_monitor_service
from app.web import STATIC_DIR


def _startup_mqtt_configuration():
    with SessionLocal() as db:
        return MqttConfigService(db).effective()


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.mqtt_service = mqtt_heartbeat_service
    mqtt_heartbeat_service.reconfigure(_startup_mqtt_configuration())
    stay_monitor_service.start()
    try:
        yield
    finally:
        mqtt_heartbeat_service.stop()
        stay_monitor_service.stop()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(account_router)
app.include_router(access_list_router)
app.include_router(audit_router)
app.include_router(auth_router)
app.include_router(barriers_router)
app.include_router(cameras_router)
app.include_router(control_boards_router)
app.include_router(mqtt_settings_router)
app.include_router(media_router)
app.include_router(operations_router)
app.include_router(live_status_router)
app.include_router(recognition_router)
app.include_router(residential_router)
app.include_router(pages_router)
app.include_router(peripheral_presets_router)
app.include_router(parking_router)
app.include_router(users_router)


@app.exception_handler(PageAuthenticationRequired)
def page_authentication_required(
    request: Request,
    _exception: PageAuthenticationRequired,
) -> RedirectResponse:
    return login_redirect_for(request)


@app.exception_handler(PasswordChangeRequired)
def password_change_required(
    _request: Request,
    _exception: PasswordChangeRequired,
) -> RedirectResponse:
    return RedirectResponse(
        "/account/change-password",
        status_code=303,
    )


@app.get("/health")
def health():
    database_status = "unknown"

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        database_status = "connected"

    except Exception:
        database_status = "disconnected"

    return {
        "application": "healthy",
        "database": database_status,
    }
