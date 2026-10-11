from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.barrier import BarrierAction, BarrierConfiguration, BarrierState
from app.models.camera import Camera


def dashboard_barrier_exceptions(db: Session, *, limit: int = 8) -> tuple[dict[str, object], ...]:
    """Return operational exceptions and manual actions, excluding routine success."""
    actions = db.scalars(
        select(BarrierAction)
        .options(joinedload(BarrierAction.camera), joinedload(BarrierAction.recognition_event))
        .where(or_(
            BarrierAction.command_status.in_(("CAMERA_REJECTED", "PUBLISH_FAILED", "TIMEOUT_UNCERTAIN")),
            BarrierAction.physical_confirmation_status == "FEEDBACK_NOT_OBSERVED",
            BarrierAction.trigger_source == "MANUAL_RELEASE",
        ))
        .order_by(BarrierAction.requested_at.desc())
        .limit(limit)
    ).unique().all()
    items = [{
        "camera_id": action.camera_id,
        "camera": action.camera.code,
        "plate": action.recognition_event.plate_text if action.recognition_event else None,
        "kind": "Manual release" if action.trigger_source == "MANUAL_RELEASE" else "Barrier exception",
        "status": action.command_status.replace("_", " ").title(),
        "physical": action.physical_confirmation_status.replace("_", " ").title(),
        "time": action.requested_at,
    } for action in actions]
    remaining = max(0, limit - len(items))
    if remaining:
        invalid_states = db.execute(
            select(BarrierState, Camera)
            .join(Camera, Camera.id == BarrierState.camera_id)
            .join(BarrierConfiguration, BarrierConfiguration.camera_id == Camera.id)
            .where(or_(BarrierState.normalized_state == "INVALID_FEEDBACK", Camera.connection_state != "ONLINE"))
            .order_by(BarrierState.updated_at.desc())
            .limit(remaining)
        ).all()
        items.extend({
            "camera_id": camera.id,
            "camera": camera.code,
            "plate": None,
            "kind": "Barrier feedback",
            "status": "Camera Offline" if camera.connection_state != "ONLINE" else "Invalid Feedback",
            "physical": state.normalized_state.replace("_", " ").title(),
            "time": state.updated_at,
        } for state, camera in invalid_states)
    return tuple(items)


__all__ = ["dashboard_barrier_exceptions"]
