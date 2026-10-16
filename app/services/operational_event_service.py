from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.barrier import BarrierAction
from app.models.operations import ParkingReconciliation, RecognitionPlateCorrection
from app.models.parking import AccessDecision, ParkingSession
from app.models.recognition_event import RecognitionEvent
from app.models.residential import VisitorReservation
from app.models.monitoring import UnifiedEvent
from app.services.event_policy_service import event_policy


def _utc(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class EventService:
    """Single policy-enforcing entry point for the operational projection."""

    def __init__(self, db: Session):
        self.db = db

    def record(
        self, *, event_type: str, occurred_at: datetime, source_type: str,
        source_id: str | UUID, display_subject: str, summary: str,
        site_id: UUID | None = None, camera_id: UUID | None = None,
        lane_id: UUID | None = None, correlation_id: UUID | None = None,
    ) -> UnifiedEvent:
        policy = event_policy(event_type)
        clean_source_id = str(source_id)
        existing = self.db.scalar(select(UnifiedEvent).where(
            UnifiedEvent.source_type == source_type,
            UnifiedEvent.source_id == clean_source_id,
            UnifiedEvent.event_type == event_type,
        ))
        if existing is not None:
            return existing
        item = UnifiedEvent(
            event_type=event_type, category=policy.category, severity=policy.severity,
            attention_required=policy.attention_required, audit_significant=policy.audit_significant,
            occurred_at=_utc(occurred_at), site_id=site_id, camera_id=camera_id, lane_id=lane_id,
            source_type=source_type, source_id=clean_source_id, correlation_id=correlation_id,
            display_subject=display_subject.strip()[:180] or event_type.replace("_", " ").title(),
            summary=summary.strip()[:500] or event_type.replace("_", " ").title(),
        )
        self.db.add(item)
        self.db.flush()
        return item


_AUDIT_EVENT_TYPES = {
    "access_list.create": "ACCESS_ENTRY_CREATED",
    "access_list.update": "ACCESS_ENTRY_UPDATED",
    "access_list.disable": "ACCESS_ENTRY_REVOKED",
    "access_list.enable": "ACCESS_ENTRY_UPDATED",
    "visitor.create": "VISITOR_REGISTERED",
    "visitor.update": "VISITOR_UPDATED",
    "visitor.cancel": "VISITOR_CANCELLED",
    "parking.access_profile_create": "ACCESS_PROFILE_CREATED",
    "parking.access_profile_update": "ACCESS_PROFILE_UPDATED",
}


class EventProjectionService:
    """Bounded, idempotent projection from existing authoritative domain tables."""

    def __init__(self, db: Session):
        self.db = db
        self.events = EventService(db)

    def project_recent(self, *, limit_per_source: int = 500) -> int:
        before = self.db.query(UnifiedEvent).count()
        self._recognitions(limit_per_source)
        self._decisions(limit_per_source)
        self._sessions(limit_per_source)
        self._barrier_actions(limit_per_source)
        self._corrections(limit_per_source)
        self._reconciliations(limit_per_source)
        self._visitors(limit_per_source)
        self._audits(limit_per_source)
        self.db.flush()
        return self.db.query(UnifiedEvent).count() - before

    def _recognitions(self, limit: int) -> None:
        rows = self.db.scalars(select(RecognitionEvent).order_by(RecognitionEvent.created_at.desc()).limit(limit)).all()
        for row in reversed(rows):
            event_type = "OFFLINE_RECOGNITION_RECOVERED" if row.source == "OFFLINE_RECOVERY" else "ANPR_RECOGNITION"
            self.events.record(event_type=event_type, occurred_at=row.captured_at or row.received_at,
                source_type="RecognitionEvent", source_id=row.id, display_subject=row.plate_text,
                summary=f"{row.plate_text} recognized ({row.direction or 'direction unknown'})",
                site_id=row.site_id, camera_id=row.camera_id, correlation_id=row.id)

    def _decisions(self, limit: int) -> None:
        rows = self.db.scalars(select(AccessDecision).order_by(AccessDecision.created_at.desc()).limit(limit)).all()
        for row in reversed(rows):
            recognition = row.recognition_event
            plate = recognition.plate_text if recognition else "Vehicle"
            match_type = {"WHITELIST": "WHITELIST_MATCH", "BLACKLIST": "BLACKLIST_MATCH"}.get(row.access_status)
            if match_type:
                self.events.record(event_type=match_type, occurred_at=row.evaluated_at, source_type="AccessDecision",
                    source_id=row.id, display_subject=plate, summary=f"{row.access_status.title()} matched for {plate}",
                    site_id=row.site_id, camera_id=row.camera_id, correlation_id=row.recognition_event_id)
            decision_type = {"ALLOW": "ACCESS_ALLOWED", "DENY": "ACCESS_DENIED", "REVIEW": "ACCESS_REVIEW"}.get(row.decision)
            if decision_type:
                self.events.record(event_type=decision_type, occurred_at=row.evaluated_at, source_type="AccessDecision",
                    source_id=row.id, display_subject=plate, summary=f"{row.decision.title()}: {row.reason_code.replace('_', ' ').title()}",
                    site_id=row.site_id, camera_id=row.camera_id, correlation_id=row.recognition_event_id)

    def _sessions(self, limit: int) -> None:
        rows = self.db.scalars(select(ParkingSession).order_by(ParkingSession.created_at.desc()).limit(limit)).all()
        for row in reversed(rows):
            plate = row.vehicle.plate_text if row.vehicle else "Vehicle"
            self.events.record(event_type="PARKING_SESSION_OPENED", occurred_at=row.entered_at,
                source_type="ParkingSession", source_id=row.id, display_subject=plate,
                summary=f"Parking session opened for {plate}", site_id=row.site_id, correlation_id=row.entry_recognition_id)
            if row.exited_at and row.exit_recognition_id:
                self.events.record(event_type="PARKING_SESSION_CLOSED", occurred_at=row.exited_at,
                    source_type="ParkingSession", source_id=row.id, display_subject=plate,
                    summary=f"Parking session closed for {plate}", site_id=row.site_id, correlation_id=row.exit_recognition_id)

    def _barrier_actions(self, limit: int) -> None:
        rows = self.db.scalars(select(BarrierAction).order_by(BarrierAction.requested_at.desc()).limit(limit)).all()
        failed = {"CAMERA_REJECTED", "PUBLISH_FAILED", "TIMEOUT_UNCERTAIN", "EXPIRED_NOT_SENT"}
        for row in reversed(rows):
            if row.trigger_source == "MANUAL_RELEASE":
                operator = row.requested_by.username if row.requested_by else "system/unknown operator"
                self.events.record(event_type="MANUAL_BARRIER_RELEASE_REQUESTED", occurred_at=row.requested_at,
                    source_type="BarrierAction", source_id=row.id, display_subject=row.camera.code,
                    summary=f"Manual barrier release requested by {operator}: {row.operator_reason or 'reason recorded in action'}",
                    site_id=row.site_id, camera_id=row.camera_id, correlation_id=row.recognition_event_id)
                if row.command_status == "CAMERA_ACKNOWLEDGED":
                    self.events.record(event_type="MANUAL_BARRIER_RELEASE_COMPLETED", occurred_at=row.camera_ack_at or row.updated_at,
                        source_type="BarrierAction", source_id=row.id, display_subject=row.camera.code,
                        summary="Camera acknowledged the barrier command; physical movement is not implied",
                        site_id=row.site_id, camera_id=row.camera_id, correlation_id=row.recognition_event_id)
            if row.command_status in failed:
                self.events.record(event_type="BARRIER_COMMAND_FAILED", occurred_at=row.updated_at,
                    source_type="BarrierAction", source_id=row.id, display_subject=row.camera.code,
                    summary=f"Barrier command status: {row.command_status}", site_id=row.site_id,
                    camera_id=row.camera_id, correlation_id=row.recognition_event_id)

    def _corrections(self, limit: int) -> None:
        rows = self.db.scalars(select(RecognitionPlateCorrection).order_by(RecognitionPlateCorrection.created_at.desc()).limit(limit)).all()
        for row in reversed(rows):
            source = row.recognition_event
            self.events.record(event_type="PLATE_CORRECTED", occurred_at=row.created_at,
                source_type="RecognitionPlateCorrection", source_id=row.id, display_subject=row.corrected_plate,
                summary=f"Plate interpretation corrected to {row.corrected_plate}", site_id=source.site_id,
                camera_id=source.camera_id, correlation_id=row.recognition_event_id)

    def _reconciliations(self, limit: int) -> None:
        rows = self.db.scalars(select(ParkingReconciliation).order_by(ParkingReconciliation.performed_at.desc()).limit(limit)).all()
        for row in reversed(rows):
            correlation = row.primary_recognition_event_id or row.secondary_recognition_event_id
            self.events.record(event_type="SESSION_RECONCILED", occurred_at=row.performed_at,
                source_type="ParkingReconciliation", source_id=row.id, display_subject=row.action_type.replace("_", " ").title(),
                summary=row.reason, site_id=row.site_id, correlation_id=correlation)

    def _visitors(self, limit: int) -> None:
        rows = self.db.scalars(select(VisitorReservation).order_by(VisitorReservation.created_at.desc()).limit(limit)).all()
        for row in reversed(rows):
            self.events.record(event_type="VISITOR_REGISTERED", occurred_at=row.created_at,
                source_type="VisitorReservation", source_id=row.id, display_subject=row.visitor_name,
                summary=f"Visitor registered for {row.plate or 'vehicle not specified'}", site_id=row.site_id)
            if row.cancelled_at:
                self.events.record(event_type="VISITOR_CANCELLED", occurred_at=row.cancelled_at,
                    source_type="VisitorReservation", source_id=row.id, display_subject=row.visitor_name,
                    summary="Visitor reservation cancelled", site_id=row.site_id)

    def _audits(self, limit: int) -> None:
        rows = self.db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)).all()
        for row in reversed(rows):
            event_type = _AUDIT_EVENT_TYPES.get(row.action)
            if not event_type:
                continue
            details = row.details if isinstance(row.details, dict) else {}
            after = details.get("after") if isinstance(details.get("after"), dict) else {}
            list_type = str(details.get("list_type") or after.get("list_type") or "").upper()
            if row.action == "access_list.create" and list_type == "BLOCK":
                event_type = "BLACKLIST_ENTRY_CREATED"
            elif row.action == "access_list.disable" and list_type == "BLOCK":
                event_type = "BLACKLIST_ENTRY_REMOVED"
            self.events.record(event_type=event_type, occurred_at=row.created_at, source_type="AuditLog",
                source_id=row.id, display_subject=(row.entity_type or "Administrative change").replace("_", " ").title(),
                summary=row.action.replace("_", " ").replace(".", " / ").title(), site_id=row.site_id)
