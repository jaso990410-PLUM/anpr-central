from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
import time
from typing import Any
from uuid import UUID

import paho.mqtt.client as mqtt
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.camera import Camera
from app.services.camera_service import (
    CREDENTIALS_REVOKED,
    OFFLINE,
    ONLINE,
    PROVISIONED,
)


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

    received_at = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    was_online = camera.connection_state == ONLINE
    camera.connection_state = ONLINE
    camera.last_seen_at = received_at
    if not was_online:
        camera.last_connected_at = received_at
    if message.agent_version is not None:
        camera.agent_version = message.agent_version
    camera.provisioning_state = PROVISIONED
    db.flush()
    return camera


def mark_timed_out_cameras(
    db: Session,
    *,
    now: datetime | None = None,
    timeout_seconds: int | None = None,
) -> int:
    checked_at = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    timeout = timeout_seconds or settings.mqtt_heartbeat_timeout_seconds
    cutoff = checked_at - timedelta(seconds=timeout)
    cameras = db.scalars(
        select(Camera).where(
            Camera.connection_state == ONLINE,
            Camera.is_enabled.is_(True),
            Camera.provisioning_state != CREDENTIALS_REVOKED,
            or_(Camera.last_seen_at.is_(None), Camera.last_seen_at <= cutoff),
        )
    ).all()
    for camera in cameras:
        camera.connection_state = OFFLINE
    if cameras:
        db.flush()
    return len(cameras)


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
        self._client_factory = client_factory or self._build_client
        self._host = host or settings.mqtt_host
        self._port = port or settings.mqtt_port
        self._heartbeat_timeout_seconds = (
            heartbeat_timeout_seconds or settings.mqtt_heartbeat_timeout_seconds
        )
        self._queue: Queue[tuple[str, bytes]] = Queue(maxsize=1000)
        self._connected = Event()
        self._stop_event = Event()
        self._lifecycle_lock = Lock()
        self._client: mqtt.Client | None = None
        self._worker: Thread | None = None
        self._running = False

    @staticmethod
    def _build_client() -> mqtt.Client:
        return mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="anpr-central",
            protocol=mqtt.MQTTv311,
        )

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def status_label(self) -> str:
        return "CONNECTED" if self.is_connected else "DISCONNECTED"

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()
            self._connected.clear()
            self._worker = Thread(
                target=self._worker_loop,
                name="anpr-mqtt-heartbeats",
                daemon=True,
            )
            self._worker.start()

            client = self._client_factory()
            self._client = client
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message
            client.reconnect_delay_set(min_delay=1, max_delay=30)
            try:
                client.connect_async(self._host, self._port, keepalive=60)
                client.loop_start()
            except Exception:
                logger.exception("Unable to start MQTT network loop")

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
            self._connected.clear()
            self._worker = None
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
            client.subscribe(STATUS_TOPIC, qos=1)
            logger.info("MQTT connected and subscribed to %s", STATUS_TOPIC)
        else:
            self._connected.clear()
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
        if not self._stop_event.is_set():
            logger.warning(
                "MQTT disconnected (%s); Paho will reconnect automatically",
                reason_code,
            )

    def _on_message(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        message: Any,
    ) -> None:
        try:
            self._queue.put_nowait((str(message.topic), bytes(message.payload)))
        except Full:
            logger.warning("MQTT heartbeat queue is full; message rejected")

    def _worker_loop(self) -> None:
        sweep_interval = max(
            1.0,
            min(15.0, self._heartbeat_timeout_seconds / 3),
        )
        next_sweep = time.monotonic() + sweep_interval
        while not self._stop_event.is_set():
            wait_seconds = max(0.1, min(1.0, next_sweep - time.monotonic()))
            try:
                topic, payload = self._queue.get(timeout=wait_seconds)
            except Empty:
                pass
            else:
                self._process_queued_heartbeat(topic, payload)

            if time.monotonic() >= next_sweep:
                self._mark_timeouts()
                next_sweep = time.monotonic() + sweep_interval

    def _process_queued_heartbeat(self, topic: str, payload: bytes) -> None:
        with self._session_factory() as db:
            try:
                process_heartbeat(db, topic, payload)
                db.commit()
            except HeartbeatRejected as error:
                db.rollback()
                logger.warning("MQTT heartbeat rejected for %s: %s", topic, error)
            except Exception:
                db.rollback()
                logger.exception("Unexpected MQTT heartbeat processing failure")

    def _mark_timeouts(self) -> None:
        with self._session_factory() as db:
            try:
                changed = mark_timed_out_cameras(
                    db,
                    timeout_seconds=self._heartbeat_timeout_seconds,
                )
                if changed:
                    db.commit()
            except Exception:
                db.rollback()
                logger.exception("Unexpected MQTT heartbeat timeout failure")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


mqtt_heartbeat_service = MqttHeartbeatService()
