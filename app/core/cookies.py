from starlette.responses import Response

from app.core.config import settings


def set_session_cookie(response: Response, raw_session_token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=raw_session_token,
        max_age=settings.session_lifetime_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def delete_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def set_login_csrf_cookie(response: Response, nonce: str) -> None:
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=nonce,
        max_age=settings.csrf_lifetime_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/login",
    )


def delete_login_csrf_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.csrf_cookie_name,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/login",
    )
