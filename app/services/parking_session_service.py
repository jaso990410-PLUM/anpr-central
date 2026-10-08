from __future__ import annotations

from datetime import timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.parking import AccessProfile, OverstayAlert, ParkingSession, ParkingSessionIssue
from app.services.parking_policy_service import DecisionContext


class ParkingSessionService:
    def __init__(self, db: Session):
        self.db = db

    def apply(self, context: DecisionContext, decision: str) -> ParkingSession | None:
        if context.vehicle is None or context.camera.lane_role not in {"ENTRY", "EXIT"}:
            return None
        active = self.db.scalar(select(ParkingSession).where(
            ParkingSession.site_id == context.site.id,
            ParkingSession.vehicle_id == context.vehicle.id,
            ParkingSession.status.in_(("OPEN", "OVERSTAY")),
        ).with_for_update())
        if context.camera.lane_role == "ENTRY":
            if context.event.source == "OFFLINE_RECOVERY" and context.access_status != "WHITELIST_ACTIVE":
                return None
            if decision != "ALLOW" and context.event.source != "OFFLINE_RECOVERY":
                return None
            if active is not None:
                self._issue(context, "DUPLICATE_ENTRY", f"Active session {active.id} already exists")
                return active
            entry = context.access_entry
            profile = self.db.get(AccessProfile, entry.access_profile_id) if entry and entry.access_profile_id else None
            if profile is not None and not profile.enabled:
                profile = None
            max_stay = entry.max_stay_minutes_override if entry and entry.max_stay_minutes_override is not None else (profile.default_max_stay_minutes if profile else None)
            due = context.evaluated_at + timedelta(minutes=max_stay) if max_stay and (profile is None or profile.overstay_monitoring_enabled) else None
            session = ParkingSession(
                site_id=context.site.id, vehicle_id=context.vehicle.id,
                access_profile_id=profile.id if profile else None,
                entry_recognition_id=context.event.id, status="OPEN", entered_at=context.evaluated_at,
                max_stay_minutes=max_stay, overstay_due_at=due, source=context.event.source,
            )
            self.db.add(session)
            self.db.flush()
            return session
        if context.event.source != "OFFLINE_RECOVERY" and decision != "ALLOW":
            return active
        if active is None:
            self._issue(context, "EXIT_WITHOUT_ENTRY", "No active stay was found")
            return None
        if context.evaluated_at < _utc(active.entered_at):
            self._issue(context, "AMBIGUOUS_CHRONOLOGY", f"Exit predates session {active.id}")
            return active
        active.exit_recognition_id = context.event.id
        active.exited_at = context.evaluated_at
        active.status = "CLOSED"
        alert = self.db.scalar(select(OverstayAlert).where(OverstayAlert.parking_session_id == active.id))
        if alert is not None and alert.status != "RESOLVED":
            alert.status = "RESOLVED"
            alert.resolved_at = context.evaluated_at
        return active

    def _issue(self, context: DecisionContext, issue_type: str, details: str) -> None:
        existing = self.db.scalar(select(ParkingSessionIssue).where(ParkingSessionIssue.recognition_event_id == context.event.id))
        if existing is None:
            self.db.add(ParkingSessionIssue(
                site_id=context.site.id, vehicle_id=context.vehicle.id if context.vehicle else None,
                recognition_event_id=context.event.id, issue_type=issue_type, status="OPEN", details=details,
            ))


def scan_overstays(db: Session, *, now) -> int:
    now = _utc(now)
    sessions = db.scalars(select(ParkingSession).where(
        ParkingSession.status == "OPEN", ParkingSession.overstay_due_at.is_not(None),
        ParkingSession.overstay_due_at <= now,
    ).with_for_update()).all()
    created = 0
    for session in sessions:
        session.status = "OVERSTAY"
        session.overstay_started_at = session.overstay_started_at or session.overstay_due_at
        alert = db.scalar(select(OverstayAlert).where(OverstayAlert.parking_session_id == session.id))
        if alert is None:
            db.add(OverstayAlert(parking_session_id=session.id, status="OPEN", detected_at=now))
            created += 1
    return created


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
