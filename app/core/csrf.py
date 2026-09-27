import hmac

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import settings
from app.core.security import hash_session_token


_CSRF_SALT = "anpr-csrf-v1"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt=_CSRF_SALT)


def generate_csrf_token(raw_session_token: str) -> str:
    """Create a signed CSRF token bound to the current browser session."""
    return _serializer().dumps(hash_session_token(raw_session_token))


def verify_csrf_token(csrf_token: str, raw_session_token: str) -> bool:
    """Validate a CSRF token and its binding without exposing the raw session."""
    try:
        session_hash = _serializer().loads(
            csrf_token,
            max_age=settings.csrf_lifetime_minutes * 60,
        )
    except (BadSignature, SignatureExpired):
        return False

    return hmac.compare_digest(
        session_hash,
        hash_session_token(raw_session_token),
    )
