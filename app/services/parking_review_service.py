from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.parking import AccessDecision, ParkingReviewItem, ParkingSession
from app.services.parking_policy_service import DecisionContext


class ParkingReviewService:
    def __init__(self, db: Session):
        self.db = db

    def apply_exit_result(
        self, context: DecisionContext, decision: AccessDecision, session: ParkingSession | None
    ) -> ParkingReviewItem | None:
        if context.event.source != "LIVE" or context.camera.lane_role != "EXIT" or context.vehicle is None:
            return None
        if decision.decision == "ALLOW":
            self._resolve_for_vehicle(context.site.id, context.vehicle.id, session, context.evaluated_at)
            return None
        if decision.decision not in {"REVIEW", "DENY"}:
            return None
        item_type = {
            "BLACKLIST": "BLACKLISTED_EXIT",
            "OVERSTAY": "OVERSTAY_EXIT",
            "EXPIRED": "EXPIRED_ACCESS_EXIT",
            "NORMAL": "POLICY_RESTRICTED_EXIT",
        }.get(_condition(decision.reason_code), "EXIT_REVIEW")
        query = select(ParkingReviewItem).where(
            ParkingReviewItem.site_id == context.site.id,
            ParkingReviewItem.vehicle_id == context.vehicle.id,
            ParkingReviewItem.item_type == item_type,
            ParkingReviewItem.reason_code == decision.reason_code,
            ParkingReviewItem.status.in_(("OPEN", "ACKNOWLEDGED")),
        )
        if session is not None:
            query = query.where(ParkingReviewItem.parking_session_id == session.id)
        else:
            query = query.where(ParkingReviewItem.parking_session_id.is_(None))
        item = self.db.scalar(query.with_for_update())
        if item is None:
            item = ParkingReviewItem(
                site_id=context.site.id, vehicle_id=context.vehicle.id,
                parking_session_id=session.id if session else None,
                recognition_event_id=context.event.id, access_decision_id=decision.id,
                item_type=item_type, reason_code=decision.reason_code, status="OPEN",
                first_triggered_at=context.evaluated_at, last_triggered_at=context.evaluated_at,
                attempt_count=1,
            )
            self.db.add(item)
        else:
            item.recognition_event_id = context.event.id
            item.access_decision_id = decision.id
            item.last_triggered_at = context.evaluated_at
            item.attempt_count += 1
        self.db.flush()
        return item

    def acknowledge(self, item_id: UUID, user_id: UUID, *, at: datetime | None = None) -> ParkingReviewItem | None:
        item = self.db.get(ParkingReviewItem, item_id)
        if item is not None and item.status == "OPEN":
            item.status = "ACKNOWLEDGED"
            item.acknowledged_at = at or datetime.now(timezone.utc)
            item.acknowledged_by_user_id = user_id
        return item

    def resolve(self, item_id: UUID, user_id: UUID, note: str, *, at: datetime | None = None) -> ParkingReviewItem | None:
        item = self.db.get(ParkingReviewItem, item_id)
        if item is not None and item.status != "RESOLVED":
            item.status = "RESOLVED"
            item.resolved_at = at or datetime.now(timezone.utc)
            item.resolved_by_user_id = user_id
            item.resolution_note = note.strip()[:1000] or None
        return item

    def _resolve_for_vehicle(self, site_id, vehicle_id, session, at):
        conditions = [ParkingReviewItem.site_id == site_id, ParkingReviewItem.vehicle_id == vehicle_id]
        if session is not None:
            conditions.append(or_(ParkingReviewItem.parking_session_id == session.id, ParkingReviewItem.parking_session_id.is_(None)))
        rows = self.db.scalars(select(ParkingReviewItem).where(
            *conditions, ParkingReviewItem.status.in_(("OPEN", "ACKNOWLEDGED"))
        )).all()
        for item in rows:
            item.status = "RESOLVED"
            item.resolved_at = at
            item.resolution_note = "Resolved by an allowed exit"


def _condition(reason_code: str) -> str | None:
    for value in ("BLACKLIST", "OVERSTAY", "EXPIRED", "NORMAL"):
        if value in reason_code:
            return value
    return None
