from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import logging
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
import time
from typing import Any
from uuid import UUID

import paho.mqtt.client as mqtt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.integrations.visionzenith.adapter import (
    NativeMessageRejected,
    VisionZenithAdapter,
)
from app.integrations.visionzenith.command_manager import (
    VisionZenithCommandManager,
)
from app.integrations.visionzenith.protocol import led_frame
from app.integrations.visionzenith.topics import (
    OFFLINE_COUNT,
    OFFLINE_RECOGNITION,
    OFFLINE_RECOVERY_REPLY,
    QUICK_RECOGNITION,
    RECOGNITION,
    KEEP_ALIVE,
    SUBSCRIPTIONS,
    parse_native_topic,
)
from app.models.camera import Camera
from app.services.mqtt_config_service import (
    EffectiveMqttConfig,
    environment_mqtt_config,
)
from app.services.recognition_ingestion_service import RecognitionWorker
from app.services.offline_recovery_service import (
    OfflineRecoveryCoordinator,
    OfflineRecoveryRejected,
)
from app.services.access_list_sync_service import AccessListSyncCoordinator
from app.services.camera_presence_service import (
    mark_camera_online,
    mark_timed_out_cameras,
)
from app.services.camera_service import CREDENTIALS_REVOKED
from app.services.control_board_runtime_service import PeripheralRuntimeCoordinator
from app.services.parking_event_service import ParkingEventCoordinator
from app.services.barrier_action_service import BarrierActionCoordinator, BarrierActionError
from app.services.barrier_status_service import BarrierStatusService


logger = logging.getLogger(__name__)
STATUS_TOPIC = "anpr/+/status"
SUPPORTED_SCHEMA_VERSION = 1


class HeartbeatRejected(ValueError):
    """A status message that is invalid or unauthorized for its camera."""


@dataclass(frozen=True)
class HeartbeatMessage:
    camera_id: UUID
    state: str
    reported_at: datetime
    agent_version: str | None


@dataclass(frozen=True)
class MqttRuntimeStatus:
    is_connected: bool
    is_enabled: bool
    host: str
    port: int
    client_id: str
    last_connected_at: datetime | None
    last_error: str | None


def parse_heartbeat(topic: str, payload: bytes) -> HeartbeatMessage:
    topic_parts = topic.split("/")
    if len(topic_parts) != 3 or topic_parts[0] != "anpr" or topic_parts[2] != "status":
        raise HeartbeatRejected("Invalid heartbeat topic")
    try:
        topic_camera_id = UUID(topic_parts[1])
    except ValueError as error:
        raise HeartbeatRejected("Invalid camera UUID in topic") from error

    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HeartbeatRejected("Malformed heartbeat JSON") from error
    if not isinstance(document, dict):
        raise HeartbeatRejected("Heartbeat payload must be a JSON object")

    schema_version = document.get("schemaVersion")
    if type(schema_version) is not int or schema_version != SUPPORTED_SCHEMA_VERSION:
        raise HeartbeatRejected("Unsupported heartbeat schema version")

    try:
        payload_camera_id = UUID(str(document.get("cameraId")))
    except (ValueError, TypeError, AttributeError) as error:
        raise HeartbeatRejected("Invalid camera UUID in payload") from error
    if payload_camera_id != topic_camera_id:
        raise HeartbeatRejected("Topic and payload camera UUID do not match")

    state = document.get("state")
    if not isinstance(state, str) or state.lower() != "online":
        raise HeartbeatRejected("Unsupported heartbeat state")

    timestamp = document.get("timestamp")
    if not isinstance(timestamp, str):
        raise HeartbeatRejected("Heartbeat timestamp is required")
    try:
        reported_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise HeartbeatRejected("Invalid heartbeat timestamp") from error
    if reported_at.tzinfo is None or reported_at.utcoffset() != timedelta(0):
        raise HeartbeatRejected("Heartbeat timestamp must be UTC")

    agent_version = document.get("agentVersion")
    if agent_version is not None:
        if not isinstance(agent_version, str):
            raise HeartbeatRejected("Agent version must be text")
        agent_version = agent_version.strip()
        if not agent_version or len(agent_version) > 100:
            raise HeartbeatRejected("Agent version is invalid")

    return HeartbeatMessage(
        camera_id=payload_camera_id,
        state=state.lower(),
        reported_at=reported_at.astimezone(timezone.utc),
        agent_version=agent_version,
    )


def process_heartbeat(
    db: Session,
    topic: str,
    payload: bytes,
    *,
    now: datetime | None = None,
) -> Camera:
    message = parse_heartbeat(topic, payload)
    camera = db.get(Camera, message.camera_id)
    if camera is None:
        raise HeartbeatRejected("Unknown camera")
    if not camera.is_enabled:
        raise HeartbeatRejected("Camera is disabled")
    if camera.provisioning_state == CREDENTIALS_REVOKED:
        raise HeartbeatRejected("Camera provisioning is revoked")

    return mark_camera_online(
        db,
        camera,
        now=now,
        agent_version=message.agent_version,
    )


class MqttHeartbeatService:
    """Non-blocking Paho lifecycle with queued database heartbeat processing."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] = SessionLocal,
        client_factory: Callable[[], mqtt.Client] | None = None,
        host: str | None = None,
        port: int | None = None,
        heartbeat_timeout_seconds: int | None = None,
    ):
        self._session_factory = session_factory
        self._client_factory = client_factory
        configuration = environment_mqtt_config()
        self._configuration = replace(
            configuration,
            host=host or configuration.host,
            port=port or configuration.port,
            heartbeat_timeout_seconds=(
                heartbeat_timeout_seconds
                or configuration.heartbeat_timeout_seconds
            ),
        )
        self._queue: Queue[tuple[str, bytes, bool]] = Queue(maxsize=1000)
        self._connected = Event()
        self._stop_event = Event()
        self._lifecycle_lock = Lock()
        self._native_probe_lock = Lock()
        self._native_probe_camera_ids: set[UUID] = set()
        self._client: mqtt.Client | None = None
        self._worker: Thread | None = None
        self._command_manager: VisionZenithCommandManager | None = None
        self._recognition_worker: RecognitionWorker | None = None
        self._offline_recovery: OfflineRecoveryCoordinator | None = None
        self._access_sync: AccessListSyncCoordinator | None = None
        self._control_board_runtime: PeripheralRuntimeCoordinator | None = None
        self._barrier_actions: BarrierActionCoordinator | None = None
        self._visionzenith = VisionZenithAdapter(
            auto_led_enabled=settings.visionzenith_auto_led_enabled
        )
        self._running = False
        self._last_connected_at: datetime | None = None
        self._last_error: str | None = None

    def _build_client(self) -> mqtt.Client:
        return mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._configuration.client_id,
            protocol=mqtt.MQTTv311,
        )

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def status_label(self) -> str:
        return "CONNECTED" if self.is_connected else "DISCONNECTED"

    @property
    def is_running(self) -> bool:
        return self._running

    def runtime_status(self) -> MqttRuntimeStatus:
        with self._lifecycle_lock:
            return MqttRuntimeStatus(
                is_connected=self.is_connected,
                is_enabled=self._configuration.is_enabled,
                host=self._configuration.host,
                port=self._configuration.port,
                client_id=self._configuration.client_id,
                last_connected_at=self._last_connected_at,
                last_error=self._last_error,
            )

    def enqueue_led_test(self, camera: Camera) -> None:
        if not self.is_connected or self._command_manager is None:
            raise RuntimeError("MQTT broker is not connected")
        if not camera.serial_number:
            raise RuntimeError("Camera serial number is required")
        if not camera.is_enabled:
            raise RuntimeError("Camera is disabled")
        if camera.provisioning_state == CREDENTIALS_REVOKED:
            raise RuntimeError("Camera provisioning is revoked")
        self._command_manager.enqueue_serial(
            camera_id=camera.id,
            serial_number=camera.serial_number,
            command_type="display_plate",
            raw_frame=led_frame("ANPR"),
            request_metadata={"diagnostic": True, "text": "ANPR"},
        )

    def enqueue_access_entry(self, entry_id: UUID, *, verify_only: bool = False) -> int:
        if self._access_sync is None:
            return 0
        return self._access_sync.enqueue_entry(entry_id, verify_only=verify_only)

    def delete_camera_access_record(self, entry_id: UUID, camera_id: UUID):
        if self._access_sync is None:
            raise RuntimeError("Access-list synchronization is unavailable")
        return self._access_sync.delete_camera_access_record(entry_id, camera_id)

    def delete_all_camera_access_records(self, camera_id: UUID):
        if self._access_sync is None:
            raise RuntimeError("Access-list synchronization is unavailable")
        return self._access_sync.delete_all_camera_access_records(camera_id)

    def test_control_board(self, board_id: UUID, action: str) -> int:
        if self._control_board_runtime is None:
            raise RuntimeError("Control-board runtime is unavailable")
        return self._control_board_runtime.test(board_id, action)

    def preview_control_board(self, board_id: UUID, rows: tuple[str, str, str, str]) -> int:
        if self._control_board_runtime is None:
            raise RuntimeError("Control-board runtime is unavailable")
        return self._control_board_runtime.send_preview_led(board_id, rows)

    def apply_control_board(self, camera_id: UUID) -> None:
        if self._control_board_runtime is None:
            raise RuntimeError("Control-board runtime is unavailable")
        self._control_board_runtime.restore_camera(camera_id, force=True)

    def read_control_board_rs485(self, board_id: UUID) -> UUID:
        if self._control_board_runtime is None:
            raise RuntimeError("Control-board runtime is unavailable")
        return self._control_board_runtime.read_rs485(board_id)

    def sync_control_board_rs485(self, board_id: UUID) -> UUID:
        if self._control_board_runtime is None:
            raise RuntimeError("Control-board runtime is unavailable")
        return self._control_board_runtime.sync_rs485(board_id)

    def recover_offline_events(self, camera_id: UUID):
        if self._offline_recovery is None:
            raise RuntimeError("Offline recognition recovery is unavailable")
        return self._offline_recovery.manual_recover(camera_id)

    def manual_barrier_open(self, *, camera_id: UUID, user_id: UUID, reason: str, review_item_id: UUID | None = None, recognition_event_id: UUID | None = None) -> UUID:
        if self._barrier_actions is None:
            raise RuntimeError("Barrier action service is unavailable")
        try:
            self._barrier_actions.start()
            return self._barrier_actions.create_manual(camera_id=camera_id, user_id=user_id, reason=reason, review_item_id=review_item_id, recognition_event_id=recognition_event_id)
        except BarrierActionError as error:
            raise RuntimeError(str(error)) from error

    def test_barrier_open(self, *, camera_id: UUID, user_id: UUID, reason: str = "Commissioning gate-open test") -> UUID:
        if self._barrier_actions is None:
            raise RuntimeError("Barrier action service is unavailable")
        try:
            self._barrier_actions.start()
            return self._barrier_actions.create_test(camera_id=camera_id, user_id=user_id, reason=reason)
        except BarrierActionError as error:
            raise RuntimeError(str(error)) from error

    def refresh_barrier_status(self, camera_id: UUID) -> int:
        if self._barrier_actions is None:
            raise RuntimeError("Barrier action service is unavailable")
        try:
            self._barrier_actions.start()
            return self._barrier_actions.reconcile_feedback(camera_id)
        except BarrierActionError as error:
            raise RuntimeError(str(error)) from error

    def start(self, configuration: EffectiveMqttConfig | None = None) -> None:
        with self._lifecycle_lock:
            if self._running:
                return
            if configuration is not None:
                self._configuration = configuration
            self._running = True
            self._stop_event.clear()
            self._connected.clear()
            with self._native_probe_lock:
                self._native_probe_camera_ids.clear()
            self._command_manager = VisionZenithCommandManager(
                session_factory=self._session_factory,
                publisher=self._publish,
                timeout_seconds=settings.visionzenith_command_timeout_seconds,
            )
            self._control_board_runtime = PeripheralRuntimeCoordinator(
                session_factory=self._session_factory,
                command_manager=self._command_manager,
                is_connected=lambda: self.is_connected,
            )
            self._control_board_runtime.start()
            self._barrier_actions = BarrierActionCoordinator(
                session_factory=self._session_factory,
                command_manager=self._command_manager,
                is_connected=lambda: self.is_connected,
            )
            self._offline_recovery = OfflineRecoveryCoordinator(
                session_factory=self._session_factory,
                command_manager=self._command_manager,
                is_connected=lambda: self.is_connected,
            )
            self._parking_events = ParkingEventCoordinator(
                self._session_factory,
                action_callback=self._dispatch_decision_actions,
                access_sync_callback=lambda entry_id: self.enqueue_access_entry(entry_id),
            )
            self._recognition_worker = RecognitionWorker(
                session_factory=self._session_factory,
                command_manager=self._command_manager,
                peripheral_event_callback=self._control_board_runtime.handle_recognition,
                business_event_callback=self._process_recognition_business_event,
                recovery_result_callback=self._offline_recovery.record_message,
            )
            self._recognition_worker.start()
            self._access_sync = AccessListSyncCoordinator(
                session_factory=self._session_factory,
                command_manager=self._command_manager,
                is_connected=lambda: self.is_connected,
            )
            self._access_sync.start()
            self._worker = Thread(
                target=self._worker_loop,
                name="anpr-mqtt-heartbeats",
                daemon=True,
            )
            self._worker.start()

            client = (
                self._client_factory()
                if self._client_factory is not None
                else self._build_client()
            )
            self._client = client
            client.on_connect = self._on_connect
            client.on_connect_fail = self._on_connect_fail
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message
            client.reconnect_delay_set(min_delay=1, max_delay=30)
            if self._configuration.username:
                password = (
                    self._configuration.password.get_secret_value()
                    if self._configuration.password is not None
                    else None
                )
                client.username_pw_set(self._configuration.username, password)
            try:
                client.connect_async(
                    self._configuration.host,
                    self._configuration.port,
                    keepalive=60,
                )
                client.loop_start()
            except Exception:
                self._last_error = "Broker connection failed"
                logger.exception("Unable to start MQTT network loop")

    def reconfigure(self, configuration: EffectiveMqttConfig) -> None:
        """Apply one configuration using the existing service lifecycle."""
        self.stop()
        with self._lifecycle_lock:
            self._configuration = configuration
            self._last_error = None
        if configuration.is_enabled:
            self.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            if not self._running:
                return
            self._stop_event.set()
            client = self._client
            if client is not None:
                try:
                    client.disconnect()
                except Exception:
                    logger.exception("Error while disconnecting MQTT client")
                try:
                    client.loop_stop()
                except Exception:
                    logger.exception("Error while stopping MQTT network loop")
            worker = self._worker
            if worker is not None:
                worker.join(timeout=5)
            if self._command_manager is not None:
                if self._access_sync is not None:
                    self._access_sync.stop()
                if self._recognition_worker is not None:
                    self._recognition_worker.stop()
                if self._control_board_runtime is not None:
                    self._control_board_runtime.stop()
                if self._barrier_actions is not None:
                    self._barrier_actions.stop()
                self._command_manager.stop()
            self._connected.clear()
            with self._native_probe_lock:
                self._native_probe_camera_ids.clear()
            self._worker = None
            self._command_manager = None
            self._recognition_worker = None
            self._offline_recovery = None
            self._access_sync = None
            self._control_board_runtime = None
            self._barrier_actions = None
            self._client = None
            self._running = False

    def _on_connect(
        self,
        client: mqtt.Client,
        _userdata: Any,
        _flags: Any,
        reason_code: Any,
        _properties: Any,
    ) -> None:
        if reason_code == 0:
            self._connected.set()
            self._last_connected_at = datetime.now(timezone.utc)
            self._last_error = None
            for topic, qos in ((STATUS_TOPIC, 1), *SUBSCRIPTIONS):
                client.subscribe(topic, qos=qos)
            logger.info("MQTT connected and subscribed to central camera topics")
        else:
            self._connected.clear()
            self._last_error = _connection_error(reason_code)
            logger.warning("MQTT connection rejected: %s", reason_code)

    def _on_disconnect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _disconnect_flags: Any,
        reason_code: Any,
        _properties: Any,
    ) -> None:
        self._connected.clear()
        with self._native_probe_lock:
            self._native_probe_camera_ids.clear()
        if not self._stop_event.is_set():
            self._last_error = "Broker disconnected"
            logger.warning(
                "MQTT disconnected (%s); Paho will reconnect automatically",
                reason_code,
            )

    def _on_connect_fail(
        self,
        _client: mqtt.Client,
        _userdata: Any,
    ) -> None:
        self._connected.clear()
        self._last_error = "Broker is unreachable"
        if not self._stop_event.is_set():
            logger.warning("MQTT broker connection attempt failed")

    def _on_message(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        message: Any,
    ) -> None:
        try:
            self._queue.put_nowait(
                (
                    str(message.topic),
                    bytes(message.payload),
                    bool(message.retain),
                )
            )
        except Full:
            logger.warning("MQTT heartbeat queue is full; message rejected")

    def _worker_loop(self) -> None:
        sweep_interval = max(
            1.0,
            min(15.0, self._configuration.heartbeat_timeout_seconds / 3),
        )
        next_sweep = time.monotonic() + sweep_interval
        while not self._stop_event.is_set():
            wait_seconds = max(0.1, min(1.0, next_sweep - time.monotonic()))
            try:
                topic, payload, retained = self._queue.get(timeout=wait_seconds)
            except Empty:
                pass
            else:
                self._process_queued_message(topic, payload, retained=retained)

            if time.monotonic() >= next_sweep:
                self._mark_timeouts()
                next_sweep = time.monotonic() + sweep_interval

    def _process_queued_message(
        self, topic: str, payload: bytes, *, retained: bool = False
    ) -> None:
        native_topic = parse_native_topic(topic)
        if native_topic is not None and native_topic.message_type in {
            QUICK_RECOGNITION,
            RECOGNITION,
            OFFLINE_RECOGNITION,
        }:
            worker = self._recognition_worker
            if worker is None:
                logger.warning("Recognition worker unavailable; event dropped")
                return
            accepted = worker.enqueue(
                native_topic.serial_number,
                native_topic.message_type,
                payload,
                retained=retained,
            )
            if (
                not accepted
                and native_topic.message_type == OFFLINE_RECOGNITION
                and self._offline_recovery is not None
            ):
                self._offline_recovery.record_message(
                    native_topic.serial_number, False, False
                )
            return
        if native_topic is not None and native_topic.message_type == OFFLINE_COUNT:
            coordinator = self._offline_recovery
            if coordinator is None:
                logger.warning("Offline recovery coordinator unavailable")
                return
            try:
                if self._recognition_worker is not None:
                    self._recognition_worker.drain()
                coordinator.handle_count(
                    native_topic.serial_number, payload, retained=retained
                )
            except OfflineRecoveryRejected as error:
                logger.warning("Offline count rejected for %s: %s", topic, error)
            return
        with self._session_factory() as db:
            try:
                if topic.startswith("anpr/"):
                    camera = process_heartbeat(db, topic, payload)
                    db.commit()
                    if self._access_sync is not None:
                        self._access_sync.enqueue_pending_for_camera(camera.id)
                    if self._control_board_runtime is not None:
                        self._control_board_runtime.restore_camera(camera.id)
                    if self._barrier_actions is not None:
                        self._barrier_actions.camera_connected(camera.id)
                    return
                else:
                    if self._command_manager is None:
                        raise NativeMessageRejected("Command manager is unavailable")
                    dispatched = self._visionzenith.dispatch(
                        db,
                        topic,
                        payload,
                        retained=retained,
                        command_manager=self._command_manager,
                    )
                db.commit()
                if (
                    dispatched == OFFLINE_RECOVERY_REPLY
                    and self._offline_recovery is not None
                    and native_topic is not None
                ):
                    self._offline_recovery.handle_ack(
                        native_topic.serial_number, payload
                    )
                if dispatched == KEEP_ALIVE:
                    camera = db.scalar(
                        select(Camera).where(
                            Camera.serial_number == native_topic.serial_number
                        )
                    )
                    if camera is not None and not retained:
                        self._probe_offline_recovery(camera.id)
                        if self._access_sync is not None:
                            self._access_sync.enqueue_pending_for_camera(camera.id)
                        if self._control_board_runtime is not None:
                            self._control_board_runtime.restore_camera(camera.id)
                        if self._barrier_actions is not None:
                            self._barrier_actions.camera_connected(camera.id)
            except (
                HeartbeatRejected,
                NativeMessageRejected,
                OfflineRecoveryRejected,
            ) as error:
                db.rollback()
                logger.warning("MQTT message rejected for %s: %s", topic, error)
            except Exception:
                db.rollback()
                logger.exception("Unexpected MQTT heartbeat processing failure")

    def _mark_timeouts(self) -> None:
        with self._session_factory() as db:
            try:
                changed = mark_timed_out_cameras(
                    db,
                    timeout_seconds=self._configuration.heartbeat_timeout_seconds,
                )
                if changed:
                    db.commit()
                    offline_camera_ids = set(
                        db.scalars(
                            select(Camera.id).where(
                                Camera.connection_state == "OFFLINE"
                            )
                        ).all()
                    )
                    with self._native_probe_lock:
                        self._native_probe_camera_ids.difference_update(
                            offline_camera_ids
                        )
                    BarrierStatusService(db).mark_offline_states_unknown()
                    db.commit()
                if self._command_manager is not None:
                    self._command_manager.mark_orphaned_commands()
                if self._offline_recovery is not None:
                    self._offline_recovery.mark_camera_offline_runs()
                    self._offline_recovery.mark_timeouts()
            except Exception:
                db.rollback()
                logger.exception("Unexpected MQTT heartbeat timeout failure")

    def _probe_offline_recovery(self, camera_id: UUID) -> None:
        coordinator = self._offline_recovery
        if coordinator is None:
            return
        with self._native_probe_lock:
            if camera_id in self._native_probe_camera_ids:
                return
            self._native_probe_camera_ids.add(camera_id)
        try:
            coordinator.probe_connection(camera_id)
        except Exception:
            with self._native_probe_lock:
                self._native_probe_camera_ids.discard(camera_id)
            raise

    def _publish(self, topic: str, payload: str, qos: int) -> bool:
        client = self._client
        if client is None or not self.is_connected:
            return False
        result = client.publish(topic, payload, qos=qos, retain=False)
        return result.rc == mqtt.MQTT_ERR_SUCCESS

    def _dispatch_decision_actions(self, decision_id: UUID) -> None:
        if self._control_board_runtime is not None:
            try:
                self._control_board_runtime.apply_decision_response(decision_id)
            except Exception:
                logger.exception("Decision presentation failed", extra={"decision_id": str(decision_id)})
        if self._barrier_actions is not None:
            try:
                self._barrier_actions.start()
                self._barrier_actions.create_automatic(decision_id)
            except Exception:
                logger.exception("Automatic barrier action failed", extra={"decision_id": str(decision_id)})

    def _process_recognition_business_event(self, event_id: UUID) -> None:
        """Keep parking and lightweight media intent isolated after event commit."""
        if self._parking_events is not None:
            try: self._parking_events.process_event(event_id)
            except Exception: logger.exception("Parking event processing failed", extra={"event_id": str(event_id)})
        try:
            from app.services.media_service import EventMediaService
            with self._session_factory() as db:
                EventMediaService(db).ensure_reference(event_id); db.commit()
        except Exception:
            logger.exception("Media reference creation failed without affecting recognition", extra={"event_id": str(event_id)})


def _connection_error(reason_code: Any) -> str:
    text = str(reason_code).lower()
    if "author" in text or "credential" in text or "password" in text:
        return "Authentication failed"
    return "Broker connection rejected"


mqtt_heartbeat_service = MqttHeartbeatService()
