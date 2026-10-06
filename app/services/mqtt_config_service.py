from dataclasses import dataclass
import re

from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.mqtt_broker_config import MqttBrokerConfig
from app.services.secret_service import SecretService


_HOST_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,253}[A-Za-z0-9])?$")
_CLIENT_ID_PATTERN = re.compile(r"^[!-~]+$")


class MqttConfigValidationError(ValueError):
    pass


@dataclass(frozen=True)
class EffectiveMqttConfig:
    is_enabled: bool
    host: str
    port: int
    username: str | None
    password: SecretStr | None
    client_id: str
    heartbeat_timeout_seconds: int
    tls_enabled: bool = False
    source: str = "environment"


@dataclass(frozen=True)
class MqttConfigInput:
    is_enabled: bool
    host: str
    port: int
    username: str | None
    client_id: str
    heartbeat_timeout_seconds: int


def environment_mqtt_config() -> EffectiveMqttConfig:
    return EffectiveMqttConfig(
        is_enabled=settings.mqtt_enabled,
        host=settings.mqtt_host,
        port=settings.mqtt_port,
        username=_optional(settings.mqtt_username),
        password=(
            settings.mqtt_password
            if settings.mqtt_password is not None
            and settings.mqtt_password.get_secret_value()
            else None
        ),
        client_id=settings.mqtt_client_id,
        heartbeat_timeout_seconds=settings.mqtt_heartbeat_timeout_seconds,
        source="environment",
    )


class MqttConfigService:
    def __init__(
        self,
        db: Session,
        *,
        secret_service: SecretService | None = None,
    ) -> None:
        self.db = db
        self.secrets = secret_service or SecretService()

    def effective(self) -> EffectiveMqttConfig:
        record = self.db.get(MqttBrokerConfig, 1)
        if record is None:
            return environment_mqtt_config()
        password = None
        if record.password_ciphertext:
            password = SecretStr(self.secrets.decrypt(record.password_ciphertext))
        return EffectiveMqttConfig(
            is_enabled=record.is_enabled,
            host=record.host,
            port=record.port,
            username=record.username,
            password=password,
            client_id=record.client_id,
            heartbeat_timeout_seconds=record.heartbeat_timeout_seconds,
            tls_enabled=record.tls_enabled,
            source="database",
        )

    def candidate(
        self,
        values: MqttConfigInput,
        *,
        submitted_password: str,
    ) -> EffectiveMqttConfig:
        clean = validate_mqtt_config(values, password=submitted_password)
        existing = self.effective()
        password = (
            SecretStr(submitted_password)
            if submitted_password
            else existing.password
        )
        return EffectiveMqttConfig(
            **clean.__dict__,
            password=password,
            source="candidate",
        )

    def save(
        self,
        values: MqttConfigInput,
        *,
        submitted_password: str,
    ) -> tuple[EffectiveMqttConfig, dict[str, bool | str]]:
        candidate = self.candidate(
            values, submitted_password=submitted_password
        )
        before = self.effective()
        record = self.db.get(MqttBrokerConfig, 1)
        if record is None:
            record = MqttBrokerConfig(id=1)
            self.db.add(record)
        record.is_enabled = candidate.is_enabled
        record.host = candidate.host
        record.port = candidate.port
        record.username = candidate.username
        record.client_id = candidate.client_id
        record.heartbeat_timeout_seconds = candidate.heartbeat_timeout_seconds
        record.tls_enabled = False
        if submitted_password:
            record.password_ciphertext = self.secrets.encrypt(submitted_password)
        elif record.password_ciphertext is None and candidate.password is not None:
            record.password_ciphertext = self.secrets.encrypt(
                candidate.password.get_secret_value()
            )
        self.db.flush()
        changes: dict[str, bool | str] = {
            "mqtt_enabled": candidate.is_enabled,
            "enabled_changed": before.is_enabled != candidate.is_enabled,
            "host_changed": before.host != candidate.host,
            "port_changed": before.port != candidate.port,
            "username_changed": before.username != candidate.username,
            "client_id_changed": before.client_id != candidate.client_id,
            "heartbeat_timeout_changed": (
                before.heartbeat_timeout_seconds
                != candidate.heartbeat_timeout_seconds
            ),
            "authentication_value_changed": bool(submitted_password),
        }
        return self.effective(), changes


def validate_mqtt_config(
    values: MqttConfigInput,
    *,
    password: str = "",
) -> MqttConfigInput:
    host = values.host.strip()
    username = _optional(values.username)
    client_id = values.client_id.strip()
    if values.is_enabled and not host:
        raise MqttConfigValidationError("Broker host is required when MQTT is enabled.")
    if host and (len(host) > 255 or not _HOST_PATTERN.fullmatch(host)):
        raise MqttConfigValidationError("Enter a valid broker hostname or IP address.")
    if not 1 <= values.port <= 65535:
        raise MqttConfigValidationError("Broker port must be between 1 and 65535.")
    if username is not None and len(username) > 255:
        raise MqttConfigValidationError("Username must contain at most 255 characters.")
    if len(password) > 1024:
        raise MqttConfigValidationError("Password must contain at most 1024 characters.")
    if values.is_enabled and not client_id:
        raise MqttConfigValidationError("Central client ID is required when MQTT is enabled.")
    if client_id and (
        len(client_id) > 128 or not _CLIENT_ID_PATTERN.fullmatch(client_id)
    ):
        raise MqttConfigValidationError(
            "Central client ID must contain 1 to 128 visible characters without spaces."
        )
    if not 5 <= values.heartbeat_timeout_seconds <= 3600:
        raise MqttConfigValidationError(
            "Camera heartbeat timeout must be between 5 and 3600 seconds."
        )
    return MqttConfigInput(
        is_enabled=values.is_enabled,
        host=host,
        port=values.port,
        username=username,
        client_id=client_id,
        heartbeat_timeout_seconds=values.heartbeat_timeout_seconds,
    )


def _optional(value: str | None) -> str | None:
    return (value or "").strip() or None
