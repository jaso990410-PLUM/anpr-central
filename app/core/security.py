import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a password using Argon2's recommended parameters."""
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Return False for a mismatch or an invalid stored Argon2 hash."""
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def generate_session_token() -> str:
    """Generate a high-entropy token suitable for an authentication cookie."""
    return secrets.token_urlsafe(32)


def hash_session_token(raw_session_token: str) -> str:
    """Create the only representation of a session token stored in the DB."""
    return hashlib.sha256(raw_session_token.encode("utf-8")).hexdigest()
