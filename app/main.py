from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from app.api.auth import router as auth_router
from app.api.pages import login_redirect_for, router as pages_router
from app.core.config import settings
from app.db.session import engine
from app.dependencies.auth import PageAuthenticationRequired
from app.web import STATIC_DIR


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(auth_router)
app.include_router(pages_router)


@app.exception_handler(PageAuthenticationRequired)
def page_authentication_required(
    request: Request,
    _exception: PageAuthenticationRequired,
) -> RedirectResponse:
    return login_redirect_for(request)


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
