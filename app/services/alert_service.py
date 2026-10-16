from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.monitoring import OperationalAlert
from app.services.audit_service import create_audit_log
from app.services.operational_event_service import EventService


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class AlertService:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def fingerprint(alert_type: str, site_id: UUID | None, component_type: str, component_id: str) -> str:
        return f"{alert_type}:{site_id or 'GLOBAL'}:{component_type}:{component_id}"[:320]

    def observe_condition(
        self, *, alert_type: str, severity: str, site_id: UUID | None,
        component_type: str, component_id: str, title: str, summary: str,
        observed_at: datetime, trigger_event_id: UUID | None = None,
    ) -> OperationalAlert:
        when = _utc(observed_at)
        fingerprint = self.fingerprint(alert_type, site_id, component_type, component_id)
        alert = self.db.scalar(select(OperationalAlert).where(OperationalAlert.fingerprint == fingerprint))
        if alert is None:
            alert = OperationalAlert(
                alert_type=alert_type, severity=severity, status="ACTIVE", site_id=site_id,
                component_type=component_type, component_id=component_id, fingerprint=fingerprint,
                title=title[:180], summary=summary[:500], first_seen_at=when, last_seen_at=when,
                opened_at=when, occurrence_count=1, trigger_event_id=trigger_event_id,
            )
            self.db.add(alert)
        elif alert.status == "RESOLVED":
            alert.status = "ACTIVE"
            alert.severity = severity
            alert.title = title[:180]
            alert.summary = summary[:500]
            alert.first_seen_at = when
            alert.last_seen_at = when
            alert.opened_at = when
            alert.occurrence_count += 1
            alert.acknowledged_at = None
            alert.acknowledged_by_user_id = None
            alert.acknowledgement_note = None
            alert.resolved_at = None
            alert.resolution_reason = None
            alert.trigger_event_id = trigger_event_id
            alert.resolution_event_id = None
        else:
            alert.last_seen_at = when
            alert.summary = summary[:500]
            alert.severity = severity
            alert.occurrence_count += 1
            if trigger_event_id and not alert.trigger_event_id:
                alert.trigger_event_id = trigger_event_id
        self.db.flush()
        return alert
    def resolve_condition(
        self, *, alert_type: str, site_id: UUID | None, component_type: str,
        component_id: str, resolved_at: datetime, reason: str,
        resolution_event_id: UUID | None = None,
    ) -> OperationalAlert | None:
        fingerprint = self.fingerprint(alert_type, site_id, component_type, component_id)
        alert = self.db.scalar(select(OperationalAlert).where(OperationalAlert.fingerprint == fingerprint))
        if alert is None or alert.status == "RESOLVED":
            return alert
        alert.status = "RESOLVED"
        alert.resolved_at = _utc(resolved_at)
        alert.resolution_reason = reason[:500]
        alert.resolution_event_id = resolution_event_id
        self.db.flush()
        return alert

    def acknowledge(
        self, alert: OperationalAlert, *, actor_user_id: UUID,
        note: str, ip_address: str | None = None, acknowledged_at: datetime | None = None,
    ) -> OperationalAlert:
        clean_note = note.strip()
        if len(clean_note) < 3:
            raise ValueError("Acknowledgement note must contain at least 3 characters")
        if alert.status == "RESOLVED":
            raise ValueError("Resolved alerts cannot be acknowledged")
        when = _utc(acknowledged_at)
        alert.status = "ACKNOWLEDGED"
        alert.acknowledged_at = when
        alert.acknowledged_by_user_id = actor_user_id
        alert.acknowledgement_note = clean_note[:1000]
        create_audit_log(self.db, action="alert.acknowledge", actor_user_id=actor_user_id,
            site_id=alert.site_id, entity_type="operational_alert", entity_id=str(alert.id),
            details={"alert_type": alert.alert_type, "component_type": alert.component_type,
                     "component_id": alert.component_id, "note": clean_note[:250]}, ip_address=ip_address)
        EventService(self.db).record(event_type="ALERT_ACKNOWLEDGED", occurred_at=when,
            source_type="OperationalAlertAcknowledgement", source_id=f"{alert.id}:{when.isoformat()}",
            display_subject=alert.title, summary=f"Alert acknowledged: {clean_note[:350]}", site_id=alert.site_id)
        self.db.flush()
        return alert
