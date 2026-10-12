from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.access_list_entry import AccessListEntry
from app.models.parking import AccessDecision, ParkingSession
from app.models.recognition_event import RecognitionEvent
from app.models.residential import VisitorReservation
from app.services.access_list_service import AccessListService
from app.services.audit_service import create_audit_log
from app.services.plate_correction_service import effective_plate


class VisitorLifecycleService:
    """Attach visitor context after the normal decision/session transaction."""

    def __init__(self, db: Session):
        self.db = db

    def process(self, event_id: UUID) -> UUID | None:
        event = self.db.get(RecognitionEvent, event_id)
        if event is None or event.source != "LIVE":
            return None
        decision = self.db.scalar(select(AccessDecision).where(AccessDecision.recognition_event_id == event.id))
        if decision is None or decision.decision != "ALLOW":
            return None
        session = self.db.scalar(select(ParkingSession).where(
            (ParkingSession.entry_recognition_id == event.id) | (ParkingSession.exit_recognition_id == event.id)
        ).with_for_update())
        if session is None:
            return None
        occurred_at = _utc(event.captured_at or event.received_at)
        if event.camera.lane_role == "ENTRY":
            reservation = self._entry_reservation(event, session, occurred_at)
            if reservation is None:
                return None
            session.visitor_reservation_id = reservation.id
            reservation.first_arrived_at = reservation.first_arrived_at or occurred_at
            reservation.visit_state = "ARRIVED"
            create_audit_log(self.db, action="visitor.arrive", site_id=event.site_id,
                             entity_type="visitor_reservation", entity_id=str(reservation.id),
                             details={"recognition_event_id": str(event.id), "parking_session_id": str(session.id)})
            self.db.flush()
            return None
        if event.camera.lane_role != "EXIT" or session.visitor_reservation_id is None or session.exit_recognition_id != event.id:
            return None
        reservation = self.db.get(VisitorReservation, session.visitor_reservation_id)
        if reservation is None:
            return None
        reservation.last_departed_at = occurred_at
        changed = None
        if reservation.visit_mode == "SINGLE_VISIT":
            reservation.visit_state = "COMPLETED"; reservation.completed_at = occurred_at
            if reservation.access_binding_mode == "TEMPORARY_ACCESS" and reservation.access_list_entry_id:
                entry = self.db.get(AccessListEntry, reservation.access_list_entry_id)
                if entry is not None and AccessListService(self.db).set_enabled(entry, False):
                    changed = entry.id
        else:
            reservation.visit_state = "DEPARTED" if occurred_at <= _utc(reservation.valid_until) else "EXPIRED"
        create_audit_log(self.db, action="visitor.depart", site_id=event.site_id,
                         entity_type="visitor_reservation", entity_id=str(reservation.id),
                         details={"recognition_event_id": str(event.id), "parking_session_id": str(session.id),
                                  "visit_state": reservation.visit_state, "temporary_access_disabled": changed is not None})
        self.db.flush()
        return changed

    def _entry_reservation(self, event: RecognitionEvent, session: ParkingSession, at: datetime) -> VisitorReservation | None:
        if session.visitor_reservation_id:
            return self.db.get(VisitorReservation, session.visitor_reservation_id)
        plate = effective_plate(self.db, event)
        return self.db.scalar(select(VisitorReservation).where(
            VisitorReservation.site_id == event.site_id,
            VisitorReservation.plate_normalized == plate,
            VisitorReservation.approval_status == "APPROVED",
            VisitorReservation.visit_state.in_(("EXPECTED", "DEPARTED")),
            VisitorReservation.valid_from <= at,
            VisitorReservation.valid_until >= at,
        ).order_by(VisitorReservation.expected_arrival_at, VisitorReservation.created_at).with_for_update())


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
