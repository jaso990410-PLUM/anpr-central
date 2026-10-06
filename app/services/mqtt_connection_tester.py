from dataclasses import dataclass
import secrets
from threading import Event
from typing import Any, Callable

import paho.mqtt.client as mqtt

from app.services.mqtt_config_service import EffectiveMqttConfig


@dataclass(frozen=True)
class MqttConnectionTestResult:
    success: bool
    message: str


class MqttConnectionTester:
    """Bounded, isolated broker handshake that never touches the live client."""

    def __init__(
        self,
        *,
        client_factory: Callable[[str], mqtt.Client] | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._client_factory = client_factory or self._build_client
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def _build_client(client_id: str) -> mqtt.Client:
        return mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
        )

    def test(self, configuration: EffectiveMqttConfig) -> MqttConnectionTestResult:
        completed = Event()
        outcome = MqttConnectionTestResult(False, "Connection timed out")
        test_id = f"{configuration.client_id}-test-{secrets.token_hex(4)}"
        client = self._client_factory(test_id)

        def on_connect(
            _client: mqtt.Client,
            _userdata: Any,
            _flags: Any,
            reason_code: Any,
            _properties: Any,
        ) -> None:
            nonlocal outcome
            if reason_code == 0:
                outcome = MqttConnectionTestResult(
                    True, "Broker handshake and authentication succeeded"
                )
            else:
                text = str(reason_code).lower()
                message = (
                    "Authentication failed"
                    if "author" in text
                    or "credential" in text
                    or "password" in text
                    else "Broker rejected the connection"
                )
                outcome = MqttConnectionTestResult(False, message)
            completed.set()

        def on_connect_fail(_client: mqtt.Client, _userdata: Any) -> None:
            nonlocal outcome
            outcome = MqttConnectionTestResult(False, "Broker is unreachable")
            completed.set()

        client.on_connect = on_connect
        client.on_connect_fail = on_connect_fail
        if configuration.username:
            password = (
                configuration.password.get_secret_value()
                if configuration.password is not None
                else None
            )
            client.username_pw_set(configuration.username, password)
        try:
            client.connect_async(
                configuration.host,
                configuration.port,
                keepalive=15,
            )
            client.loop_start()
            completed.wait(self._timeout_seconds)
            return outcome
        except OSError:
            return MqttConnectionTestResult(False, "Broker is unreachable")
        finally:
            try:
                client.disconnect()
            except Exception:
                pass
            try:
                client.loop_stop()
            except Exception:
                pass


mqtt_connection_tester = MqttConnectionTester()
