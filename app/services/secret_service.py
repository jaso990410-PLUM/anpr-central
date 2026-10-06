import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class SecretDecryptionError(ValueError):
    pass


class SecretService:
    """Authenticated encryption for database-held application secrets."""

    def __init__(self, bootstrap_secret: str | None = None) -> None:
        source = (bootstrap_secret or settings.secret_key).encode("utf-8")
        derived_key = hashlib.sha256(
            b"anpr-central:mqtt-secret:v1:" + source
        ).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(derived_key))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as error:
            raise SecretDecryptionError(
                "Stored MQTT password cannot be decrypted"
            ) from error
