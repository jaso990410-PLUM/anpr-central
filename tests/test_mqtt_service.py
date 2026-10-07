from datetime import datetime, timedelta, timezone
import importlib
import json
import uuid

import pytest
from pydantic import SecretStr
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.camera import Camera
from app.models.site import Site
from app.services.camera_service import (
    CONNECTING,
    CREDENTIALS_REVOKED,
    OFFLINE,
    ONLINE,
    PROVISIONED,
)
from app.services.mqtt_service import (
    HeartbeatRejected,
    MqttHeartbeatService,
    STATUS_TOPIC,
    mark_timed_out_cameras,
    parse_heartbeat,
    process_heartbeat,
)
from app.integrations.visionzenith.topics import SUBSCRIPTIONS
from app.services.mqtt_config_service import EffectiveMqttConfig


class FakeRecognitionWorker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes, bool]] = []

    def enqueue(
        self,
        serial_number: str,
        message_type: str,
        payload: bytes,
        *,
        retained: bool,
    ) -> bool:
        self.calls.append((serial_number, message_type, payload, retained))
        return True


def create_camera(db: Session, *, enabled: bool = True) -> Camera:
    site = Site(
        code="MAIN",
        name="Main Site",
        timezone="Asia/Kuala_Lumpur",
        is_active=True,
    )
    camera = Camera(
        site=site,
        code="MAIN-ENTRY-01",
        name="Main Entry",
        direction="ENTRY",
        is_enabled=enabled,
    )
    db.add(camera)
    db.commit()
    return camera


def heartbeat_payload(
    camera_id: uuid.UUID,
    *,
    schema_version: int = 1,
    payload_camera_id: uuid.UUID | None = None,
    agent_version: str | None = "1.0.0",
) -> bytes:
    document = {
        "schemaVersion": schema_version,
        "cameraId": str(payload_camera_id or camera_id),
        "state": "online",
        "timestamp": "2026-09-27T10:00:00Z",
    }
    if agent_version is not None:
        document["agentVersion"] = agent_version
    return json.dumps(document).encode()


def test_native_recognition_is_delegated_without_database_work() -> None:
    database_opened = False

    def forbidden_session_factory():
        nonlocal database_opened
        database_opened = True
        raise AssertionError("MQTT worker must not persist recognition inline")

    service = MqttHeartbeatService(session_factory=forbidden_session_factory)
    recognition_worker = FakeRecognitionWorker()
    service._recognition_worker = recognition_worker

    service._process_queued_message(
        "device/RA5-SERIAL/message/up/quick_ivs_result",
        b'{"payload":{}}',
        retained=False,
    )

    assert not database_opened
    assert recognition_worker.calls == [
        ("RA5-SERIAL", "quick_ivs_result", b'{"payload":{}}', False)
    ]


def test_native_heartbeat_enqueues_safe_pending_access_sync(db: Session) -> None:
    target = create_camera(db)
    target.serial_number = "RA5-ACCESS"
    db.commit()
    queued: list[uuid.UUID] = []
    probed: list[uuid.UUID] = []

    class FakeAccessSync:
        def enqueue_pending_for_camera(self, camera_id):
            queued.append(camera_id)
            return 1

    class FakeOfflineRecovery:
        def probe_connection(self, camera_id):
            probed.append(camera_id)

    service = MqttHeartbeatService(session_factory=lambda: Session(db.get_bind(), expire_on_commit=False))
    service._command_manager = object()
    service._access_sync = FakeAccessSync()
    service._offline_recovery = FakeOfflineRecovery()
    service._process_queued_message(
        "device/RA5-ACCESS/message/up/keep_alive",
        b'{"sn":"RA5-ACCESS"}',
        retained=True,
    )
    assert probed == []
    service._process_queued_message(
        "device/RA5-ACCESS/message/up/keep_alive",
        b'{"sn":"RA5-ACCESS"}',
    )
    service._process_queued_message(
        "device/RA5-ACCESS/message/up/keep_alive",
        b'{"sn":"RA5-ACCESS"}',
    )

    assert queued == [target.id, target.id]
    assert probed == [target.id]

    service._on_disconnect(None, None, None, 1, None)
    service._process_queued_message(
        "device/RA5-ACCESS/message/up/keep_alive",
        b'{"sn":"RA5-ACCESS"}',
    )
    assert probed == [target.id, target.id]


def test_valid_heartbeat_sets_online_seen_connected_and_agent_version(
    db: Session,
) -> None:
    camera = create_camera(db)
    received_at = datetime(2026, 9, 27, 10, 0, 5, tzinfo=timezone.utc)

    processed = process_heartbeat(
        db,
        f"anpr/{camera.id}/status",
        heartbeat_payload(camera.id),
        now=received_at,
    )

    assert processed.id == camera.id
    assert camera.connection_state == ONLINE
    assert camera.last_seen_at == received_at
    assert camera.last_connected_at == received_at
    assert camera.agent_version == "1.0.0"
    assert camera.provisioning_state == PROVISIONED


def test_first_valid_heartbeat_completes_connecting_provisioning(
    db: Session,
) -> None:
    camera = create_camera(db)
    camera.provisioning_state = CONNECTING
    db.commit()

    process_heartbeat(
        db,
        f"anpr/{camera.id}/status",
        heartbeat_payload(camera.id),
    )

    assert camera.provisioning_state == PROVISIONED
    assert camera.connection_state == ONLINE


def test_online_heartbeat_updates_last_seen_without_resetting_last_connected(
    db: Session,
) -> None:
    camera = create_camera(db)
    first_connection = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
    camera.connection_state = ONLINE
    camera.provisioning_state = PROVISIONED
    camera.last_seen_at = first_connection
    camera.last_connected_at = first_connection
    db.commit()
    next_seen = first_connection + timedelta(seconds=15)

    process_heartbeat(
        db,
        f"anpr/{camera.id}/status",
        heartbeat_payload(camera.id, agent_version="1.1.0"),
        now=next_seen,
    )

    assert camera.last_seen_at == next_seen
    assert camera.last_connected_at == first_connection
    assert camera.agent_version == "1.1.0"


def test_heartbeat_timeout_marks_online_camera_offline_and_reconnects(
    db: Session,
) -> None:
    camera = create_camera(db)
    first_connection = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
    camera.connection_state = ONLINE
    camera.provisioning_state = PROVISIONED
    camera.last_seen_at = first_connection
    camera.last_connected_at = first_connection
    db.commit()
    timeout_check = first_connection + timedelta(seconds=46)

    changed = mark_timed_out_cameras(
        db,
        now=timeout_check,
        timeout_seconds=45,
    )

    assert changed == 1
    assert camera.connection_state == OFFLINE
    assert camera.provisioning_state == PROVISIONED

    reconnected_at = timeout_check + timedelta(seconds=5)
    process_heartbeat(
        db,
        f"anpr/{camera.id}/status",
        heartbeat_payload(camera.id),
        now=reconnected_at,
    )
    assert camera.connection_state == ONLINE
    assert camera.last_connected_at == reconnected_at


def test_unknown_camera_heartbeat_is_rejected(db: Session) -> None:
    camera_id = uuid.uuid4()

    with pytest.raises(HeartbeatRejected, match="Unknown camera"):
        process_heartbeat(
            db,
            f"anpr/{camera_id}/status",
            heartbeat_payload(camera_id),
        )


@pytest.mark.parametrize(
    ("topic", "payload", "message"),
    [
        ("anpr/not-a-uuid/status", b"{}", "Invalid camera UUID in topic"),
        (f"anpr/{uuid.uuid4()}/status", b"not-json", "Malformed heartbeat JSON"),
        (f"anpr/{uuid.uuid4()}/status", b"[]", "JSON object"),
    ],
)
def test_malformed_heartbeats_are_rejected(
    topic: str,
    payload: bytes,
    message: str,
) -> None:
    with pytest.raises(HeartbeatRejected, match=message):
        parse_heartbeat(topic, payload)


def test_topic_payload_camera_mismatch_is_rejected() -> None:
    topic_camera_id = uuid.uuid4()
    payload_camera_id = uuid.uuid4()

    with pytest.raises(HeartbeatRejected, match="do not match"):
        parse_heartbeat(
            f"anpr/{topic_camera_id}/status",
            heartbeat_payload(
                topic_camera_id,
                payload_camera_id=payload_camera_id,
            ),
        )


def test_unsupported_schema_version_is_rejected() -> None:
    camera_id = uuid.uuid4()

    with pytest.raises(HeartbeatRejected, match="Unsupported"):
        parse_heartbeat(
            f"anpr/{camera_id}/status",
            heartbeat_payload(camera_id, schema_version=2),
        )


@pytest.mark.parametrize("blocked_state", ["disabled", "revoked"])
def test_disabled_or_revoked_camera_heartbeat_is_rejected(
    db: Session,
    blocked_state: str,
) -> None:
    camera = create_camera(db, enabled=blocked_state != "disabled")
    if blocked_state == "revoked":
        camera.provisioning_state = CREDENTIALS_REVOKED
        db.commit()

    with pytest.raises(HeartbeatRejected):
        process_heartbeat(
            db,
            f"anpr/{camera.id}/status",
            heartbeat_payload(camera.id),
        )

    assert camera.connection_state is None
    assert camera.last_seen_at is None


class FakeMqttClient:
    def __init__(self, *, fail_connect: bool = False):
        self.fail_connect = fail_connect
        self.on_connect = None
        self.on_connect_fail = None
        self.on_disconnect = None
        self.on_message = None
        self.reconnect_delays = None
        self.connect_args = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.subscriptions: list[tuple[str, int]] = []
        self.credentials = None

    def reconnect_delay_set(self, *, min_delay: int, max_delay: int) -> None:
        self.reconnect_delays = (min_delay, max_delay)

    def connect_async(self, host: str, port: int, *, keepalive: int) -> None:
        if self.fail_connect:
            raise OSError("broker unavailable")
        self.connect_args = (host, port, keepalive)

    def loop_start(self) -> None:
        self.loop_started = True

    def loop_stop(self) -> None:
        self.loop_stopped = True

    def disconnect(self) -> None:
        self.disconnected = True

    def subscribe(self, topic: str, *, qos: int) -> None:
        self.subscriptions.append((topic, qos))

    def username_pw_set(self, username: str, password: str | None) -> None:
        self.credentials = (username, password)


def test_mqtt_service_lifecycle_subscribes_and_stops_cleanly() -> None:
    fake_client = FakeMqttClient()
    service = MqttHeartbeatService(
        session_factory=lambda: pytest.fail("database should not be used"),
        client_factory=lambda: fake_client,
        host="broker.test",
        port=1884,
        heartbeat_timeout_seconds=45,
    )

    service.start()
    assert fake_client.reconnect_delays == (1, 30)
    assert fake_client.connect_args == ("broker.test", 1884, 60)
    assert fake_client.loop_started
    assert fake_client.on_connect is not None
    fake_client.on_connect(
        fake_client,
        None,
        None,
        ReasonCode(PacketTypes.CONNACK, "Success"),
        None,
    )
    assert service.is_connected
    assert service.status_label == "CONNECTED"
    assert fake_client.subscriptions == [(STATUS_TOPIC, 1), *SUBSCRIPTIONS]

    service.stop()
    assert fake_client.disconnected
    assert fake_client.loop_stopped
    assert not service.is_connected


def test_initial_mqtt_outage_does_not_crash_service() -> None:
    fake_client = FakeMqttClient(fail_connect=True)
    service = MqttHeartbeatService(
        session_factory=lambda: pytest.fail("database should not be used"),
        client_factory=lambda: fake_client,
    )

    service.start()
    assert not service.is_connected
    assert service.status_label == "DISCONNECTED"
    service.stop()


def test_runtime_reconfigure_reuses_one_lifecycle_and_disable_stops() -> None:
    clients = [FakeMqttClient(), FakeMqttClient()]
    service = MqttHeartbeatService(
        session_factory=lambda: pytest.fail("database should not be used"),
        client_factory=lambda: clients.pop(0),
    )
    first = EffectiveMqttConfig(
        True,
        "broker-one.internal",
        1883,
        "anpr",
        SecretStr("secret-one"),
        "central-one",
        45,
    )
    first_client = clients[0]

    service.reconfigure(first)
    assert service.is_running
    assert first_client.connect_args == ("broker-one.internal", 1883, 60)
    assert first_client.credentials == ("anpr", "secret-one")
    first_client.on_connect(first_client, None, None, 0, None)
    status = service.runtime_status()
    assert status.is_connected
    assert status.last_connected_at is not None
    assert status.last_error is None

    disabled = EffectiveMqttConfig(
        False,
        "broker-one.internal",
        1883,
        "anpr",
        SecretStr("secret-one"),
        "central-one",
        45,
    )
    service.reconfigure(disabled)
    assert not service.is_running
    assert first_client.disconnected
    assert first_client.loop_stopped
    assert service.runtime_status().is_enabled is False

    second = EffectiveMqttConfig(
        True,
        "broker-two.internal",
        2883,
        None,
        None,
        "central-two",
        60,
    )
    second_client = clients[0]
    service.reconfigure(second)
    assert service.is_running
    assert second_client.connect_args == ("broker-two.internal", 2883, 60)
    second_client.on_connect(second_client, None, None, "Not authorized", None)
    assert service.runtime_status().last_error == "Authentication failed"
    second_client.on_connect_fail(second_client, None)
    assert service.runtime_status().last_error == "Broker is unreachable"
    service.stop()


class FakeEngine:
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement) -> None:
            return None

    def connect(self) -> "FakeEngine.Connection":
        return self.Connection()


class FakeLifecycleService:
    def __init__(self):
        self.started = False
        self.stopped = False

    def reconfigure(self, _configuration) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def test_fastapi_lifecycle_starts_stops_mqtt_and_outage_preserves_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = importlib.import_module("app.main")
    lifecycle_service = FakeLifecycleService()
    monkeypatch.setattr(main_module, "mqtt_heartbeat_service", lifecycle_service)
    monkeypatch.setattr(main_module.settings, "mqtt_enabled", True)
    monkeypatch.setattr(main_module, "engine", FakeEngine())
    monkeypatch.setattr(
        main_module,
        "_startup_mqtt_configuration",
        lambda: object(),
    )

    with TestClient(main_module.app) as client:
        assert lifecycle_service.started
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {
            "application": "healthy",
            "database": "connected",
        }

    assert lifecycle_service.stopped
