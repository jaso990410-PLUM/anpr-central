import pytest
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.mqtt_broker_config import MqttBrokerConfig
from app.services.mqtt_config_service import (
    EffectiveMqttConfig,
    MqttConfigInput,
    MqttConfigService,
    MqttConfigValidationError,
)
from app.services.mqtt_connection_tester import MqttConnectionTester
from app.services.secret_service import SecretService


def form_values(**overrides) -> MqttConfigInput:
    values = {
        "is_enabled": True,
        "host": "broker.internal",
        "port": 1883,
        "username": "anpr",
        "client_id": "anpr-central",
        "heartbeat_timeout_seconds": 45,
    }
    values.update(overrides)
    return MqttConfigInput(**values)


def test_secret_service_uses_authenticated_ciphertext() -> None:
    service = SecretService("bootstrap-secret-with-at-least-32-characters")
    ciphertext = service.encrypt("mqtt-password")

    assert ciphertext != "mqtt-password"
    assert "mqtt-password" not in ciphertext
    assert service.decrypt(ciphertext) == "mqtt-password"


def test_environment_fallback_and_database_override(db: Session) -> None:
    service = MqttConfigService(db)
    fallback = service.effective()
    assert fallback.source == "environment"
    assert fallback.host == settings.mqtt_host

    saved, _changes = service.save(
        form_values(host="db-broker.internal", port=2883),
        submitted_password="database-password",
    )
    db.commit()

    assert saved.source == "database"
    assert saved.host == "db-broker.internal"
    assert saved.port == 2883
    assert saved.password is not None
    assert saved.password.get_secret_value() == "database-password"
    record = db.get(MqttBrokerConfig, 1)
    assert record is not None
    assert record.password_ciphertext != "database-password"
    assert "database-password" not in (record.password_ciphertext or "")


def test_blank_password_preserves_existing_encrypted_secret(db: Session) -> None:
    service = MqttConfigService(db)
    service.save(form_values(), submitted_password="first-password")
    db.commit()
    record = db.get(MqttBrokerConfig, 1)
    assert record is not None
    original_ciphertext = record.password_ciphertext

    effective, _changes = service.save(
        form_values(host="new-broker.internal"), submitted_password=""
    )
    db.commit()

    assert record.password_ciphertext == original_ciphertext
    assert effective.password is not None
    assert effective.password.get_secret_value() == "first-password"


def test_new_password_replaces_ciphertext(db: Session) -> None:
    service = MqttConfigService(db)
    service.save(form_values(), submitted_password="first-password")
    db.commit()
    record = db.get(MqttBrokerConfig, 1)
    assert record is not None
    original_ciphertext = record.password_ciphertext

    effective, changes = service.save(
        form_values(), submitted_password="second-password"
    )
    db.commit()

    assert record.password_ciphertext != original_ciphertext
    assert effective.password is not None
    assert effective.password.get_secret_value() == "second-password"
    assert changes["authentication_value_changed"] is True


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (form_values(host="https://broker/path"), "hostname or IP"),
        (form_values(port=0), "between 1 and 65535"),
        (form_values(client_id="bad client"), "without spaces"),
        (form_values(heartbeat_timeout_seconds=4), "between 5 and 3600"),
    ],
)
def test_invalid_configuration_is_rejected(
    db: Session, values: MqttConfigInput, message: str
) -> None:
    with pytest.raises(MqttConfigValidationError, match=message):
        MqttConfigService(db).candidate(values, submitted_password="")


class FakeTestClient:
    def __init__(self, reason_code=0, *, fail_connect: bool = False) -> None:
        self.reason_code = reason_code
        self.fail_connect = fail_connect
        self.on_connect = None
        self.on_connect_fail = None
        self.loop_stopped = False
        self.disconnected = False
        self.credentials = None

    def username_pw_set(self, username, password) -> None:
        self.credentials = (username, password)

    def connect_async(self, _host, _port, *, keepalive) -> None:
        if self.fail_connect:
            raise OSError("unreachable")

    def loop_start(self) -> None:
        if self.on_connect is not None:
            self.on_connect(self, None, None, self.reason_code, None)

    def disconnect(self) -> None:
        self.disconnected = True

    def loop_stop(self) -> None:
        self.loop_stopped = True


def test_connection_test_success_and_cleanup() -> None:
    client = FakeTestClient()
    tester = MqttConnectionTester(client_factory=lambda _client_id: client)
    configuration = EffectiveMqttConfig(
        True,
        "broker.internal",
        1883,
        "anpr",
        SecretStr("secret"),
        "anpr-central",
        45,
    )

    result = tester.test(configuration)

    assert result.success
    assert client.credentials == ("anpr", "secret")
    assert client.disconnected
    assert client.loop_stopped


def test_connection_test_authentication_failure_and_unreachable_cleanup() -> None:
    rejected = FakeTestClient("Not authorized")
    tester = MqttConnectionTester(client_factory=lambda _client_id: rejected)
    configuration = EffectiveMqttConfig(
        True, "broker.internal", 1883, "anpr", None, "anpr-central", 45
    )
    result = tester.test(configuration)
    assert not result.success
    assert result.message == "Authentication failed"
    assert rejected.disconnected and rejected.loop_stopped

    unreachable = FakeTestClient(fail_connect=True)
    tester = MqttConnectionTester(client_factory=lambda _client_id: unreachable)
    result = tester.test(configuration)
    assert not result.success
    assert result.message == "Broker is unreachable"
    assert unreachable.disconnected and unreachable.loop_stopped
