from datetime import timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.dependencies import get_db
from app.dependencies.auth import require_permission
from app.models.camera import Camera
from app.models.user import User
from app.services.camera_service import connection_state
from app.services.mqtt_service import mqtt_heartbeat_service


router = APIRouter()


@router.get("/api/status/live")
def live_status(
    _user: User = Depends(require_permission("cameras.view")),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    runtime = mqtt_heartbeat_service.runtime_status()
    cameras = db.scalars(select(Camera).order_by(Camera.code)).all()
    camera_values = [
        {
            "id": str(camera.id),
            "code": camera.code,
            "state": connection_state(camera),
            "last_seen": (
                camera.last_seen_at.astimezone(timezone.utc).isoformat()
                if camera.last_seen_at and camera.last_seen_at.tzinfo
                else (
                    camera.last_seen_at.replace(tzinfo=timezone.utc).isoformat()
                    if camera.last_seen_at
                    else None
                )
            ),
        }
        for camera in cameras
    ]
    return {
        "mqtt": {
            "connected": runtime.is_connected,
            "enabled": runtime.is_enabled,
            "broker": f"{runtime.host}:{runtime.port}",
        },
        "cameras_online": sum(
            camera["state"] == "ONLINE" for camera in camera_values
        ),
        "cameras": camera_values,
    }
