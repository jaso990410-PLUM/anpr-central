from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.access_list_entry import AccessListEntry
from app.models.camera import Camera
from app.models.operations import ParkingReconciliation
from app.models.parking import AccessProfile, OverstayAlert, ParkingReviewItem, ParkingSession, ParkingSessionIssue
from app.models.recognition_event import RecognitionEvent
from app.models.vehicle import Vehicle
from app.services.plate_correction_service import effective_plate


class ReconciliationError(ValueError):
    pass


class ReconciliationConflict(ReconciliationError):
    pass


class ParkingReconciliationService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def apply(
        self,
        *,
        action_type: str,
        action_token: str,
        primary_event_id: UUID,
        user_id: UUID,
        reason: str,
        session_id: UUID | None = None,
        secondary_event_id: UUID | None = None,
        review_item_id: UUID | None = None,
    ) -> tuple[ParkingReconciliation, bool]:
        token = action_token.strip()
        if not token or len(token) > 64:
            raise ReconciliationError("The reconciliation request token is invalid. Reload and try again.")
        existing = self.db.scalar(select(ParkingReconciliation).where(ParkingReconciliation.action_token == token))
        if existing is not None:
            return existing, False
        clean_reason = reason.strip()
        if len(clean_reason) < 3:
            raise ReconciliationError("Enter a meaningful reconciliation reason.")
        event = self.db.scalar(select(RecognitionEvent).where(RecognitionEvent.id == primary_event_id).with_for_update())
        if event is None:
            raise ReconciliationError("Recognition event was not found.")
        previous_state: dict[str, object] = {"session": None}
        if action_type == "LINK_EXIT_TO_OPEN_SESSION":
            selected = self._session(session_id)
            previous_state = self._snapshot(selected)
            session = self._link_exit(event, selected)
        elif action_type == "CREATE_OPEN_SESSION_FROM_ENTRY":
            session = self._create_open(event)
        elif action_type == "CREATE_CLOSED_SESSION_FROM_ENTRY_EXIT_PAIR":
            session = self._create_closed(event, self._event(secondary_event_id))
        elif action_type == "MARK_DUPLICATE_ENTRY":
            selected = self._session(session_id)
            previous_state = self._snapshot(selected)
            session = self._mark_duplicate(event, selected)
        elif action_type == "ATTACH_RECOVERED_EVENT_TO_SESSION":
            selected = self._session(session_id)
            previous_state = self._snapshot(selected)
            session = self._attach_recovered(event, selected)
        elif action_type == "RESOLVE_WITHOUT_MATCH":
            session = None
            self._resolve_issue(event.id, clean_reason, required=True)
        else:
            raise ReconciliationError("Select a supported reconciliation action.")
        review = self.db.get(ParkingReviewItem, review_item_id) if review_item_id else None
        if review_item_id and (review is None or review.site_id != event.site_id):
            raise ReconciliationError("Review item does not belong to this site.")
        if action_type != "RESOLVE_WITHOUT_MATCH":
            self._resolve_issue(event.id, clean_reason)
        if review is not None and review.status != "RESOLVED":
            review.status = "RESOLVED"
            review.resolved_at = datetime.now(timezone.utc)
            review.resolved_by_user_id = user_id
            review.resolution_note = clean_reason[:1000]
        record = ParkingReconciliation(
            site_id=event.site_id,
            action_type=action_type,
            action_token=token,
            primary_recognition_event_id=event.id,
            secondary_recognition_event_id=secondary_event_id,
            parking_session_id=session.id if session else None,
            parking_review_item_id=review.id if review else None,
            previous_state=previous_state,
            resulting_state=self._snapshot(session),
            reason=clean_reason[:1000],
            performed_by_user_id=user_id,
        )
        self.db.add(record)
        self.db.flush()
        return record, True

    def _event(self, event_id: UUID | None) -> RecognitionEvent:
        if event_id is None:
            raise ReconciliationError("Select the second recognition event.")
        event = self.db.scalar(select(RecognitionEvent).where(RecognitionEvent.id == event_id).with_for_update())
        if event is None:
            raise ReconciliationError("The selected recognition event was not found.")
        return event

    def _session(self, session_id: UUID | None) -> ParkingSession:
        if session_id is None:
            raise ReconciliationError("Select a parking session.")
        session = self.db.scalar(select(ParkingSession).where(ParkingSession.id == session_id).with_for_update())
        if session is None:
            raise ReconciliationError("The selected parking session was not found.")
        return session

    def _camera_role(self, event: RecognitionEvent) -> str:
        camera = self.db.get(Camera, event.camera_id)
        return camera.lane_role if camera else "UNSPECIFIED"

    def _vehicle(self, event: RecognitionEvent) -> Vehicle:
        plate = effective_plate(self.db, event)
        vehicle = self.db.scalar(select(Vehicle).where(Vehicle.plate_text == plate))
        if vehicle is None:
            vehicle = Vehicle(plate_text=plate)
            self.db.add(vehicle)
            self.db.flush()
        return vehicle

    def _event_time(self, event: RecognitionEvent) -> datetime:
        return _utc(event.captured_at or event.received_at)

    def _link_exit(self, event: RecognitionEvent, session: ParkingSession) -> ParkingSession:
        if self._camera_role(event) != "EXIT":
            raise ReconciliationError("Only an EXIT-role recognition can close a session.")
        if session.site_id != event.site_id:
            raise ReconciliationError("The recognition and session belong to different sites.")
        if session.status not in {"OPEN", "OVERSTAY"} or session.exit_recognition_id is not None:
            raise ReconciliationConflict("Session was already changed by another operator.")
        exit_at = self._event_time(event)
        if exit_at < _utc(session.entered_at):
            raise ReconciliationError("Selected exit is earlier than the session entry.")
        if self._event_in_session(event.id):
            raise ReconciliationConflict("Selected recognition has already been reconciled to another session.")
        session.exit_recognition_id = event.id
        session.exited_at = exit_at
        session.status = "CLOSED"
        self._resolve_overstay(session, exit_at)
        return session

    def _create_open(self, event: RecognitionEvent) -> ParkingSession:
        if self._camera_role(event) != "ENTRY":
            raise ReconciliationError("Only an ENTRY-role recognition can create an open session.")
        if self._event_in_session(event.id):
            raise ReconciliationConflict("Selected recognition already belongs to a parking session.")
        vehicle = self._vehicle(event)
        self._ensure_no_active(event.site_id, vehicle.id)
        return self._new_session(event, vehicle, None)

    def _create_closed(self, first: RecognitionEvent, second: RecognitionEvent) -> ParkingSession:
        if self._camera_role(first) == "EXIT" and self._camera_role(second) == "ENTRY":
            entry, exit_event = second, first
        else:
            entry, exit_event = first, second
        if self._camera_role(entry) != "ENTRY" or self._camera_role(exit_event) != "EXIT":
            raise ReconciliationError("Select an ENTRY event followed by an EXIT event.")
        if entry.site_id != exit_event.site_id:
            raise ReconciliationError("Entry and exit events belong to different sites.")
        entered_at, exited_at = self._event_time(entry), self._event_time(exit_event)
        if exited_at < entered_at:
            raise ReconciliationError("Selected exit is earlier than the entry.")
        if self._event_in_session(entry.id) or self._event_in_session(exit_event.id):
            raise ReconciliationConflict("One of the selected recognitions already belongs to a parking session.")
        vehicle = self._vehicle(entry)
        if effective_plate(self.db, exit_event) != vehicle.plate_text:
            raise ReconciliationError("Entry and exit effective plates do not match.")
        overlap = self.db.scalar(select(ParkingSession).where(
            ParkingSession.site_id == entry.site_id,
            ParkingSession.vehicle_id == vehicle.id,
            ParkingSession.entered_at <= exited_at,
            or_(ParkingSession.exited_at.is_(None), ParkingSession.exited_at >= entered_at),
        ).with_for_update())
        if overlap is not None:
            raise ReconciliationConflict("This would overlap another parking session for the vehicle.")
        return self._new_session(entry, vehicle, exit_event)

    def _mark_duplicate(self, event: RecognitionEvent, session: ParkingSession) -> ParkingSession:
        if self._camera_role(event) != "ENTRY":
            raise ReconciliationError("Only an ENTRY-role recognition can be marked as a duplicate entry.")
        if session.site_id != event.site_id or session.status not in {"OPEN", "OVERSTAY"}:
            raise ReconciliationConflict("The selected active session is no longer available.")
        if self._event_time(event) < _utc(session.entered_at):
            raise ReconciliationError("Duplicate entry cannot predate the original session entry.")
        return session

    def _attach_recovered(self, event: RecognitionEvent, session: ParkingSession) -> ParkingSession:
        if event.source != "OFFLINE_RECOVERY":
            raise ReconciliationError("Only an offline-recovered recognition can use this action.")
        if session.site_id != event.site_id:
            raise ReconciliationError("The recovered event and session belong to different sites.")
        if self._camera_role(event) == "EXIT" and session.status in {"OPEN", "OVERSTAY"}:
            return self._link_exit(event, session)
        return session

    def _new_session(self, entry: RecognitionEvent, vehicle: Vehicle, exit_event: RecognitionEvent | None) -> ParkingSession:
        access = self.db.scalar(select(AccessListEntry).where(AccessListEntry.site_id == entry.site_id, AccessListEntry.vehicle_id == vehicle.id))
        profile = self.db.get(AccessProfile, access.access_profile_id) if access and access.access_profile_id else None
        if profile is not None and not profile.enabled:
            profile = None
        maximum = access.max_stay_minutes_override if access and access.max_stay_minutes_override is not None else (profile.default_max_stay_minutes if profile else None)
        entered_at = self._event_time(entry)
        exited_at = self._event_time(exit_event) if exit_event else None
        session = ParkingSession(
            site_id=entry.site_id,
            vehicle_id=vehicle.id,
            access_profile_id=profile.id if profile else None,
            entry_recognition_id=entry.id,
            exit_recognition_id=exit_event.id if exit_event else None,
            status="CLOSED" if exit_event else "OPEN",
            entered_at=entered_at,
            exited_at=exited_at,
            max_stay_minutes=maximum,
            overstay_due_at=entered_at + timedelta(minutes=maximum) if maximum and (profile is None or profile.overstay_monitoring_enabled) else None,
            effective_normal_exit_policy=profile.normal_exit_policy if profile else "USE_SITE_POLICY",
            effective_expired_exit_policy=profile.expired_exit_policy if profile else "USE_SITE_POLICY",
            effective_overstay_exit_policy=profile.overstay_exit_policy if profile else "USE_SITE_POLICY",
            effective_blacklist_exit_policy=profile.blacklist_exit_policy if profile else "USE_SITE_POLICY",
            effective_overstay_exit_grace_minutes=profile.overstay_exit_grace_minutes if profile else 0,
            source="RECONCILED",
        )
        self.db.add(session)
        self.db.flush()
        return session

    def _ensure_no_active(self, site_id: UUID, vehicle_id: UUID) -> None:
        active = self.db.scalar(select(ParkingSession).where(
            ParkingSession.site_id == site_id,
            ParkingSession.vehicle_id == vehicle_id,
            ParkingSession.status.in_(("OPEN", "OVERSTAY")),
        ).with_for_update())
        if active is not None:
            raise ReconciliationConflict("This vehicle already has an active parking session.")

    def _event_in_session(self, event_id: UUID) -> bool:
        return self.db.scalar(select(ParkingSession.id).where(or_(
            ParkingSession.entry_recognition_id == event_id,
            ParkingSession.exit_recognition_id == event_id,
        )).limit(1)) is not None

    def _resolve_issue(self, event_id: UUID, reason: str, *, required: bool = False) -> None:
        issue = self.db.scalar(select(ParkingSessionIssue).where(ParkingSessionIssue.recognition_event_id == event_id).with_for_update())
        if issue is None:
            if required:
                raise ReconciliationError("No open session exception exists for this recognition.")
            return
        if issue.status != "OPEN":
            raise ReconciliationConflict("This exception was already resolved by another operator.")
        issue.status = "RESOLVED"
        issue.resolved_at = datetime.now(timezone.utc)
        issue.details = f"{issue.details or ''}\nResolved by reconciliation: {reason}".strip()[:2000]

    def _resolve_overstay(self, session: ParkingSession, at: datetime) -> None:
        alert = self.db.scalar(select(OverstayAlert).where(OverstayAlert.parking_session_id == session.id))
        if alert is not None and alert.status != "RESOLVED":
            alert.status = "RESOLVED"
            alert.resolved_at = at

    @staticmethod
    def _snapshot(session: ParkingSession | None) -> dict[str, object]:
        if session is None:
            return {"session": None}
        return {
            "session_id": str(session.id),
            "status": session.status,
            "entry_recognition_id": str(session.entry_recognition_id),
            "exit_recognition_id": str(session.exit_recognition_id) if session.exit_recognition_id else None,
            "entered_at": _utc(session.entered_at).isoformat(),
            "exited_at": _utc(session.exited_at).isoformat() if session.exited_at else None,
        }


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
