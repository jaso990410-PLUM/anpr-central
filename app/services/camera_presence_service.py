from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.camera import Camera
from app.services.camera_service import (
    CREDENTIALS_REVOKED,
    OFFLINE,
    ONLINE,
    PROVISIONED,
)


def mark_camera_online(
    db: Session,
    camera: Camera,
    *,
    now: datetime | None = None,
    agent_version: str | None = None,
) -> Camera:
    received_at = as_utc(now) if now is not None else datetime.now(timezone.utc)
    was_online = camera.connection_state == ONLINE
    camera.connection_state = ONLINE
    camera.last_seen_at = received_at
    if not was_online:
        camera.last_connected_at = received_at
    if agent_version is not None:
        camera.agent_version = agent_version
    camera.provisioning_state = PROVISIONED
    db.flush()
    return camera


def mark_timed_out_cameras(
    db: Session,
    *,
    now: datetime | None = None,
    timeout_seconds: int | None = None,
) -> int:
    checked_at = as_utc(now) if now is not None else datetime.now(timezone.utc)
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


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
