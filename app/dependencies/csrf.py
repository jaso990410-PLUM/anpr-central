from typing import Annotated

from fastapi import Form, HTTPException, Request, status

from app.core.config import settings
from app.core.csrf import verify_csrf_token


def require_session_csrf(
    request: Request,
    csrf_token: Annotated[str | None, Form()] = None,
) -> str:
    """Validate CSRF for browser forms authenticated by a session cookie."""
    raw_session_token = request.cookies.get(settings.session_cookie_name)
    if (
        not raw_session_token
        or not csrf_token
        or not verify_csrf_token(csrf_token, raw_session_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid CSRF token",
        )
    return raw_session_token
