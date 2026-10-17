from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import re
from typing import Iterable
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from app.core.config import settings
from app.models.access_list_entry import AccessListEntry
from app.models.barrier import BarrierAction
from app.models.camera import Camera
from app.models.media import MediaSource, RecordingSegment
from app.models.monitoring import OperationalAlert, UnifiedEvent
from app.models.offline_recovery_run import OfflineRecoveryRun
from app.models.operations import ParkingReconciliation, RecognitionPlateCorrection
from app.models.parking import AccessDecision, AccessProfile, ParkingSession
from app.models.recognition_event import RecognitionEvent
from app.models.site import Site


MAX_REPORT_DAYS = 366
STAY_BUCKETS = (
    ("Under 30 min", 0, 30),
    ("30–60 min", 30, 60),
    ("1–2 hr", 60, 120),
    ("2–4 hr", 120, 240),
    ("4–8 hr", 240, 480),
    ("8+ hr", 480, None),
)
DECISIONS = ("ALLOW", "DENY", "REVIEW", "NO_ACTION")
INCIDENT_TYPES = {
    "CAMERA_OFFLINE",
    "RECORDER_STOPPED",
    "RECORDING_STALE",
    "MQTT_DISCONNECTED",
    "DATABASE_UNAVAILABLE",
    "DATABASE_SLOW",
    "STORAGE_LOW",
    "STORAGE_CRITICAL",
    "STORAGE_UNAVAILABLE",
    "ACCESS_SYNC_MISMATCH",
    "OFFLINE_BACKLOG",
    "BARRIER_CONTROL_DEGRADED",
    "PERIPHERAL_COMMAND_FAILED",
    "BARRIER_COMMAND_FAILED",
    "COMPONENT_FAULT",
}
RECOVERY_TYPES = {
    "CAMERA_ONLINE",
    "RECORDER_STARTED",
    "RECORDING_RESUMED",
    "MQTT_RECONNECTED",
    "DATABASE_RECOVERED",
    "STORAGE_RECOVERED",
    "ACCESS_SYNC_RESTORED",
    "OFFLINE_BACKLOG_CLEARED",
    "BARRIER_CONTROL_RESTORED",
    "COMPONENT_RECOVERED",
}


class ReportValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ReportQuery:
    site: Site
    camera: Camera | None
    preset: str
    start_at: datetime
    end_at: datetime
    date_from: date
    date_to: date
    timezone: ZoneInfo | timezone
    granularity: str

    @property
    def range_label(self) -> str:
        if self.date_from == self.date_to:
            return self.date_from.strftime("%d %b %Y")
        return f"{self.date_from:%d %b %Y} – {self.date_to:%d %b %Y}"

    @property
    def timezone_name(self) -> str:
        return self.site.timezone

    def params(self) -> dict[str, str]:
        values = {
            "site": str(self.site.id),
            "period": self.preset,
            "date_from": self.date_from.isoformat(),
            "date_to": self.date_to.isoformat(),
        }
        if self.camera is not None:
            values["camera"] = str(self.camera.id)
        return values


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _zone(name: str) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def _friendly(value: str) -> str:
    labels = {
        "NO_ACTION": "No Action",
        "TIMEOUT_UNCERTAIN": "Timeout / Uncertain",
        "CAMERA_ACKNOWLEDGED": "Camera Acknowledged",
        "PUBLISH_FAILED": "Publish Failed",
        "CAMERA_REJECTED": "Camera Rejected",
        "EXPIRED_NOT_SENT": "Expired / Not Sent",
        "SUPPRESSED_ALREADY_OPEN": "Suppressed / Already Open",
        "NOT_CONFIGURED": "Not Configured",
        "UNKNOWN_OR_INACTIVE_ACCESS": "Unknown / Inactive Access",
    }
    return labels.get(value, value.replace("_", " ").title())


def _duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    return f"{secs}s"


def _advance(value: datetime, granularity: str) -> datetime:
    if granularity == "hour":
        return value + timedelta(hours=1)
    if granularity == "day":
        return value + timedelta(days=1)
    year, month = value.year, value.month + 1
    if month == 13:
        year, month = year + 1, 1
    return value.replace(year=year, month=month, day=1)


def _floor(value: datetime, granularity: str) -> datetime:
    if granularity == "hour":
        return value.replace(minute=0, second=0, microsecond=0)
    if granularity == "day":
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    return value.replace(month=value.month, day=1, hour=0, minute=0, second=0, microsecond=0)


def _bucket_label(value: datetime, granularity: str) -> str:
    if granularity == "hour":
        return value.strftime("%d %b %H:00")
    if granularity == "day":
        return value.strftime("%d %b")
    return value.strftime("%b %Y")


def _bucket_starts(query: ReportQuery) -> list[datetime]:
    local_start = query.start_at.astimezone(query.timezone)
    local_end = query.end_at.astimezone(query.timezone)
    current = _floor(local_start, query.granularity)
    values: list[datetime] = []
    while current < local_end and len(values) < 400:
        values.append(current)
        current = _advance(current, query.granularity)
    return values


def _bucket_key(value: datetime, query: ReportQuery) -> datetime:
    return _floor(_utc(value).astimezone(query.timezone), query.granularity)


def _chart(rows: list[dict[str, object]], fields: tuple[str, ...]) -> list[dict[str, object]]:
    maximum = max((int(row.get(field, 0) or 0) for row in rows for field in fields), default=0)
    for row in rows:
        row["bars"] = {
            field: round((int(row.get(field, 0) or 0) / maximum) * 100, 2) if maximum else 0
            for field in fields
        }
    return rows


class ReportingService:
    def __init__(self, db: Session):
        self.db = db

    def sites(self) -> tuple[Site, ...]:
        return tuple(self.db.scalars(select(Site).where(Site.is_active.is_(True)).order_by(Site.code)).all())

    def cameras(self, site_id: UUID) -> tuple[Camera, ...]:
        return tuple(self.db.scalars(select(Camera).where(Camera.site_id == site_id).order_by(Camera.code)).all())

    def query(
        self,
        *,
        site_id: UUID | None,
        period: str = "last_7_days",
        date_from: str = "",
        date_to: str = "",
        camera_id: UUID | None = None,
        now: datetime | None = None,
    ) -> ReportQuery:
        sites = self.sites()
        if not sites:
            raise ReportValidationError("No active reporting site is available.")
        site = self.db.get(Site, site_id) if site_id else next(
            (item for item in sites if item.code == settings.default_site_code), sites[0]
        )
        if site is None or not site.is_active:
            raise ReportValidationError("Select an active site.")
        camera = self.db.get(Camera, camera_id) if camera_id else None
        if camera_id and (camera is None or camera.site_id != site.id):
            raise ReportValidationError("Select a camera at the reporting site.")

        zone = _zone(site.timezone)
        local_now = _utc(now or datetime.now(timezone.utc)).astimezone(zone)
        today = local_now.date()
        allowed = {"today", "yesterday", "last_7_days", "this_month", "last_30_days", "custom"}
        preset = period if period in allowed else "last_7_days"
        if preset == "today":
            first, last, end_local = today, today, local_now
        elif preset == "yesterday":
            first = last = today - timedelta(days=1)
            end_local = datetime.combine(today, time.min, zone)
        elif preset == "this_month":
            first, last, end_local = today.replace(day=1), today, local_now
        elif preset == "last_30_days":
            first, last, end_local = today - timedelta(days=29), today, local_now
        elif preset == "custom":
            try:
                first, last = date.fromisoformat(date_from), date.fromisoformat(date_to)
            except ValueError as error:
                raise ReportValidationError("Choose valid start and end dates.") from error
            end_local = datetime.combine(last + timedelta(days=1), time.min, zone)
        else:
            first, last, end_local = today - timedelta(days=6), today, local_now

        if first > last:
            raise ReportValidationError("Start date must not be after end date.")
        if (last - first).days + 1 > MAX_REPORT_DAYS:
            raise ReportValidationError(f"Interactive reports are limited to {MAX_REPORT_DAYS} days.")
        start_local = datetime.combine(first, time.min, zone)
        if end_local <= start_local:
            raise ReportValidationError("The report period must contain time.")
        span = end_local - start_local
        granularity = "hour" if span <= timedelta(hours=48) else "day" if span <= timedelta(days=120) else "month"
        return ReportQuery(
            site=site,
            camera=camera,
            preset=preset,
            start_at=start_local.astimezone(timezone.utc),
            end_at=end_local.astimezone(timezone.utc),
            date_from=first,
            date_to=last,
            timezone=zone,
            granularity=granularity,
        )

    def traffic(self, query: ReportQuery) -> dict[str, object]:
        entry_event = aliased(RecognitionEvent)
        exit_event = aliased(RecognitionEvent)
        statement = (
            select(
                ParkingSession.entered_at,
                ParkingSession.exited_at,
                ParkingSession.status,
                entry_event.camera_id.label("entry_camera_id"),
                exit_event.camera_id.label("exit_camera_id"),
            )
            .join(entry_event, entry_event.id == ParkingSession.entry_recognition_id)
            .outerjoin(exit_event, exit_event.id == ParkingSession.exit_recognition_id)
            .where(
                ParkingSession.site_id == query.site.id,
                ParkingSession.entered_at < query.end_at,
                or_(ParkingSession.exited_at.is_(None), ParkingSession.exited_at >= query.start_at),
            )
        )
        rows = self.db.execute(statement).all()
        selected = [
            row for row in rows
            if query.camera is None or row.entry_camera_id == query.camera.id or row.exit_camera_id == query.camera.id
        ]
        entries = [
            row for row in selected
            if query.start_at <= _utc(row.entered_at) < query.end_at
            and (query.camera is None or row.entry_camera_id == query.camera.id)
        ]
        exits = [
            row for row in selected
            if row.exited_at is not None and query.start_at <= _utc(row.exited_at) < query.end_at
            and (query.camera is None or row.exit_camera_id == query.camera.id)
        ]
        occupancy_rows = [row for row in selected if query.camera is None or row.entry_camera_id == query.camera.id]
        baseline = sum(
            1 for row in occupancy_rows
            if _utc(row.entered_at) <= query.start_at
            and (row.exited_at is None or _utc(row.exited_at) > query.start_at)
        )
        transitions: list[tuple[datetime, int]] = []
        for row in occupancy_rows:
            entered = _utc(row.entered_at)
            exited = _utc(row.exited_at) if row.exited_at else None
            if query.start_at < entered < query.end_at:
                transitions.append((entered, 1))
            if exited is not None and query.start_at < exited < query.end_at:
                transitions.append((exited, -1))
        # At the same instant, an exiting session no longer contributes while an
        # entering session does. Apply exits first to avoid a false transient peak.
        transitions.sort(key=lambda item: (item[0], item[1]))
        current = baseline
        peak, peak_at = baseline, query.start_at
        for occurred, delta in transitions:
            current = max(0, current + delta)
            if current > peak:
                peak, peak_at = current, occurred

        buckets = _bucket_starts(query)
        entry_counts = Counter(_bucket_key(row.entered_at, query) for row in entries)
        exit_counts = Counter(_bucket_key(row.exited_at, query) for row in exits if row.exited_at)
        trend = _chart([
            {"key": bucket, "label": _bucket_label(bucket, query.granularity),
             "entries": entry_counts[bucket], "exits": exit_counts[bucket]}
            for bucket in buckets
        ], ("entries", "exits"))

        occupancy_points: list[dict[str, object]] = []
        for bucket in buckets:
            next_bucket = _advance(bucket, query.granularity).astimezone(timezone.utc)
            point_time = min(next_bucket, query.end_at)
            value = sum(
                1 for row in occupancy_rows
                if _utc(row.entered_at) <= point_time
                and (row.exited_at is None or _utc(row.exited_at) > point_time)
            )
            occupancy_points.append({"label": _bucket_label(bucket, query.granularity), "occupancy": value})
        _chart(occupancy_points, ("occupancy",))

        completed_rows = [
            row for row in selected
            if row.exited_at is not None and query.start_at <= _utc(row.exited_at) < query.end_at
        ]
        completed_seconds = [
            max(0, (_utc(row.exited_at) - _utc(row.entered_at)).total_seconds())
            for row in completed_rows if row.exited_at is not None
        ]
        average = sum(completed_seconds) / len(completed_seconds) if completed_seconds else None
        stay_counts = []
        for label, minimum, maximum in STAY_BUCKETS:
            count = sum(1 for seconds in completed_seconds if seconds >= minimum * 60 and (maximum is None or seconds < maximum * 60))
            stay_counts.append({"label": label, "count": count})
        _chart(stay_counts, ("count",))

        recognition_rows = self.db.execute(
            select(RecognitionEvent.source, func.count())
            .where(
                RecognitionEvent.site_id == query.site.id,
                func.coalesce(RecognitionEvent.captured_at, RecognitionEvent.received_at) >= query.start_at,
                func.coalesce(RecognitionEvent.captured_at, RecognitionEvent.received_at) < query.end_at,
                *([RecognitionEvent.camera_id == query.camera.id] if query.camera else []),
            )
            .group_by(RecognitionEvent.source)
        ).all()
        recognitions = {source: count for source, count in recognition_rows}
        correction_filters = [
            RecognitionPlateCorrection.created_at >= query.start_at,
            RecognitionPlateCorrection.created_at < query.end_at,
            RecognitionEvent.site_id == query.site.id,
        ]
        if query.camera:
            correction_filters.append(RecognitionEvent.camera_id == query.camera.id)
        correction_count = self.db.scalar(
            select(func.count()).select_from(RecognitionPlateCorrection)
            .join(RecognitionEvent, RecognitionEvent.id == RecognitionPlateCorrection.recognition_event_id)
            .where(*correction_filters)
        ) or 0
        return {
            "entries": len(entries), "exits": len(exits), "peak_occupancy": peak,
            "peak_occupancy_at": peak_at.astimezone(query.timezone).strftime("%d %b %Y · %H:%M"),
            "average_stay_seconds": average, "average_stay": _duration(average),
            "peak_entry": self._peak_entry(entry_counts, query),
            "trend": trend, "occupancy": occupancy_points, "stay_distribution": stay_counts,
            "recognition_live": recognitions.get("LIVE", 0),
            "recognition_recovered": recognitions.get("OFFLINE_RECOVERY", 0),
            "corrections": correction_count,
        }

    def access(self, query: ReportQuery) -> dict[str, object]:
        conditions = [
            AccessDecision.site_id == query.site.id,
            AccessDecision.evaluated_at >= query.start_at,
            AccessDecision.evaluated_at < query.end_at,
        ]
        if query.camera:
            conditions.append(AccessDecision.camera_id == query.camera.id)
        rows = self.db.execute(select(AccessDecision.evaluated_at, AccessDecision.decision,
                                      AccessDecision.reason_code, AccessDecision.access_status,
                                      AccessDecision.access_entry_id).where(*conditions)).all()
        counts = Counter(row.decision for row in rows)
        buckets = _bucket_starts(query)
        trend_map: dict[datetime, Counter[str]] = defaultdict(Counter)
        for row in rows:
            trend_map[_bucket_key(row.evaluated_at, query)][row.decision] += 1
        trend = _chart([
            {"label": _bucket_label(bucket, query.granularity), **{key.lower(): trend_map[bucket][key] for key in DECISIONS}}
            for bucket in buckets
        ], tuple(key.lower() for key in DECISIONS))
        denial_counter = Counter(row.reason_code for row in rows if row.decision == "DENY")
        denied_total = sum(denial_counter.values())
        reasons = [
            {"code": code, "label": _friendly(code), "count": count,
             "percent": round(count * 100 / denied_total, 1) if denied_total else 0}
            for code, count in denial_counter.most_common()
        ]

        profile_conditions = list(conditions)
        profile_rows = self.db.execute(
            select(AccessProfile.name, func.count())
            .select_from(AccessDecision)
            .outerjoin(AccessListEntry, AccessListEntry.id == AccessDecision.access_entry_id)
            .outerjoin(AccessProfile, AccessProfile.id == AccessListEntry.access_profile_id)
            .where(*profile_conditions)
            .group_by(AccessProfile.name)
            .order_by(func.count().desc())
        ).all()
        profiles = [{"label": name or "Unclassified", "count": count} for name, count in profile_rows]
        _chart(profiles, ("count",))
        blacklist = sum(1 for row in rows if str(row.access_status).startswith("BLACKLIST"))
        watch_conditions = [UnifiedEvent.site_id == query.site.id, UnifiedEvent.event_type == "WATCHLIST_MATCH",
                            UnifiedEvent.occurred_at >= query.start_at, UnifiedEvent.occurred_at < query.end_at]
        if query.camera:
            watch_conditions.append(UnifiedEvent.camera_id == query.camera.id)
        watchlist = self.db.scalar(select(func.count()).select_from(UnifiedEvent).where(*watch_conditions)) or 0
        return {
            "counts": {key: counts[key] for key in DECISIONS}, "trend": trend,
            "denial_reasons": reasons, "profiles": profiles,
            "blacklist": blacklist, "watchlist": watchlist,
        }

    def operations(self, query: ReportQuery) -> dict[str, object]:
        action_conditions = [BarrierAction.site_id == query.site.id,
                             BarrierAction.requested_at >= query.start_at,
                             BarrierAction.requested_at < query.end_at]
        if query.camera:
            action_conditions.append(BarrierAction.camera_id == query.camera.id)
        actions = self.db.execute(select(BarrierAction.requested_at, BarrierAction.trigger_source,
                                         BarrierAction.command_status).where(*action_conditions)).all()
        outcome_counts = Counter(row.command_status for row in actions)
        outcomes = [{"label": _friendly(code), "count": count} for code, count in outcome_counts.most_common()]
        _chart(outcomes, ("count",))
        buckets = _bucket_starts(query)
        action_trend = Counter(_bucket_key(row.requested_at, query) for row in actions)
        manual_trend = Counter(_bucket_key(row.requested_at, query) for row in actions if row.trigger_source == "MANUAL_RELEASE")
        trend = _chart([
            {"label": _bucket_label(bucket, query.granularity), "commands": action_trend[bucket], "manual": manual_trend[bucket]}
            for bucket in buckets
        ], ("commands", "manual"))

        correction_conditions = [RecognitionPlateCorrection.created_at >= query.start_at,
                                 RecognitionPlateCorrection.created_at < query.end_at,
                                 RecognitionEvent.site_id == query.site.id]
        if query.camera:
            correction_conditions.append(RecognitionEvent.camera_id == query.camera.id)
        corrections = self.db.execute(
            select(RecognitionPlateCorrection.created_at)
            .join(RecognitionEvent, RecognitionEvent.id == RecognitionPlateCorrection.recognition_event_id)
            .where(*correction_conditions)
        ).scalars().all()
        reconciliation_conditions = [ParkingReconciliation.site_id == query.site.id,
                                     ParkingReconciliation.performed_at >= query.start_at,
                                     ParkingReconciliation.performed_at < query.end_at]
        reconciliation_statement = select(ParkingReconciliation.performed_at).where(*reconciliation_conditions)
        if query.camera:
            reconciliation_statement = reconciliation_statement.outerjoin(
                RecognitionEvent, RecognitionEvent.id == ParkingReconciliation.primary_recognition_event_id
            ).where(RecognitionEvent.camera_id == query.camera.id)
        reconciliations = self.db.execute(reconciliation_statement).scalars().all()
        correction_trend = Counter(_bucket_key(item, query) for item in corrections)
        reconciliation_trend = Counter(_bucket_key(item, query) for item in reconciliations)
        operator_trend = _chart([
            {"label": _bucket_label(bucket, query.granularity), "corrections": correction_trend[bucket],
             "reconciliations": reconciliation_trend[bucket]}
            for bucket in buckets
        ], ("corrections", "reconciliations"))

        recovery_conditions = [Camera.site_id == query.site.id,
                               OfflineRecoveryRun.started_at >= query.start_at,
                               OfflineRecoveryRun.started_at < query.end_at]
        if query.camera:
            recovery_conditions.append(OfflineRecoveryRun.camera_id == query.camera.id)
        recovery_rows = self.db.execute(
            select(OfflineRecoveryRun.started_at, OfflineRecoveryRun.status,
                   OfflineRecoveryRun.events_inserted, OfflineRecoveryRun.duplicates_skipped,
                   OfflineRecoveryRun.invalid_skipped)
            .join(Camera, Camera.id == OfflineRecoveryRun.camera_id)
            .where(*recovery_conditions)
        ).all()
        return {
            "barrier_commands": len(actions),
            "manual_releases": sum(1 for row in actions if row.trigger_source == "MANUAL_RELEASE"),
            "corrections": len(corrections), "reconciliations": len(reconciliations),
            "outcomes": outcomes, "trend": trend, "operator_trend": operator_trend,
            "recovery": {
                "runs": len(recovery_rows),
                "imported": sum(row.events_inserted for row in recovery_rows),
                "duplicates": sum(row.duplicates_skipped for row in recovery_rows),
                "skipped": sum(row.invalid_skipped for row in recovery_rows),
                "failures": sum(1 for row in recovery_rows if row.status in {"FAILED", "ERROR", "TIMEOUT"}),
            },
        }

    def reliability(self, query: ReportQuery) -> dict[str, object]:
        event_conditions = [UnifiedEvent.site_id == query.site.id,
                            UnifiedEvent.occurred_at >= query.start_at,
                            UnifiedEvent.occurred_at < query.end_at]
        source_ids: tuple[UUID, ...] = ()
        if query.camera:
            source_ids = tuple(self.db.scalars(
                select(MediaSource.id).where(MediaSource.camera_id == query.camera.id)
            ).all())
            identity_prefixes = [f"CAMERA:{query.camera.id}:%", f"ACCESS_SYNC:{query.camera.id}:%",
                                 f"OFFLINE_RECOVERY:{query.camera.id}:%", f"BARRIER_CONTROL:{query.camera.id}:%",
                                 f"BARRIER_FEEDBACK:{query.camera.id}:%"]
            identity_prefixes.extend(f"RECORDING_SOURCE:{source_id}:%" for source_id in source_ids)
            identity_prefixes.extend(f"MEDIA:{source_id}:%" for source_id in source_ids)
            event_conditions.append(or_(UnifiedEvent.camera_id == query.camera.id,
                                        *(UnifiedEvent.source_id.like(prefix) for prefix in identity_prefixes)))
        events = self.db.execute(
            select(UnifiedEvent.occurred_at, UnifiedEvent.event_type, UnifiedEvent.summary,
                   UnifiedEvent.severity, UnifiedEvent.camera_id).where(*event_conditions)
        ).all()
        incidents = [row for row in events if row.event_type in INCIDENT_TYPES]
        incident_types = Counter(row.event_type for row in incidents)
        incident_rows = [{"label": _friendly(code), "count": count} for code, count in incident_types.most_common()]
        _chart(incident_rows, ("count",))
        buckets = _bucket_starts(query)
        incident_trend = Counter(_bucket_key(row.occurred_at, query) for row in incidents)
        recovery_trend = Counter(_bucket_key(row.occurred_at, query) for row in events if row.event_type in RECOVERY_TYPES)
        trend = _chart([
            {"label": _bucket_label(bucket, query.granularity), "incidents": incident_trend[bucket], "recoveries": recovery_trend[bucket]}
            for bucket in buckets
        ], ("incidents", "recoveries"))

        alert_conditions = [OperationalAlert.site_id == query.site.id,
                            OperationalAlert.opened_at >= query.start_at,
                            OperationalAlert.opened_at < query.end_at]
        if query.camera:
            alert_conditions.append(OperationalAlert.component_id.in_((str(query.camera.id), *(str(item) for item in source_ids))))
        alerts = self.db.execute(
            select(OperationalAlert.alert_type, OperationalAlert.severity, OperationalAlert.opened_at,
                   OperationalAlert.resolved_at, OperationalAlert.component_type).where(*alert_conditions)
        ).all()
        severity = Counter(row.severity for row in alerts)
        alert_types = Counter(row.alert_type for row in alerts)
        alert_rows = [{"label": _friendly(code), "count": count} for code, count in alert_types.most_common()]
        _chart(alert_rows, ("count",))
        resolved_durations = [
            max(0, (_utc(row.resolved_at) - _utc(row.opened_at)).total_seconds())
            for row in alerts if row.resolved_at is not None
        ]

        gaps = [self._gap_duration(row.summary) for row in events if row.event_type == "RECORDING_GAP"]
        gap_durations = [value for value in gaps if value is not None]
        coverage = self._media_coverage(query)
        return {
            "incidents": len(incidents), "incident_types": incident_rows, "trend": trend,
            "camera_incidents": sum(1 for row in incidents if row.event_type == "CAMERA_OFFLINE"),
            "recorder_incidents": sum(1 for row in incidents if row.event_type == "RECORDER_STOPPED"),
            "recording_stale": sum(1 for row in incidents if row.event_type == "RECORDING_STALE"),
            "alerts_opened": len(alerts),
            "alerts_resolved": sum(1 for row in alerts if row.resolved_at is not None),
            "alert_severity": {"WARNING": severity["WARNING"], "CRITICAL": severity["CRITICAL"]},
            "alert_types": alert_rows,
            "observed_alert_duration": _duration(sum(resolved_durations)) if resolved_durations else "—",
            "gap_count": sum(1 for row in events if row.event_type == "RECORDING_GAP"),
            "gap_duration": _duration(sum(gap_durations)) if gap_durations else "—",
            "longest_gap": _duration(max(gap_durations)) if gap_durations else "—",
            "coverage": coverage,
        }

    def landing(self, query: ReportQuery) -> dict[str, object]:
        traffic = self.traffic(query)
        access = self.access(query)
        operations = self.operations(query)
        reliability = self.reliability(query)
        return {
            "traffic": traffic, "access": access, "operations": operations,
            "reliability": reliability,
        }

    @staticmethod
    def _peak_entry(counts: Counter[datetime], query: ReportQuery) -> dict[str, object] | None:
        if not counts:
            return None
        bucket, count = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
        end = _advance(bucket, query.granularity)
        if query.granularity == "hour":
            label = f"{bucket:%d %b · %H:%M}–{end:%H:%M}"
        elif query.granularity == "day":
            label = bucket.strftime("%d %b %Y")
        else:
            label = bucket.strftime("%b %Y")
        return {"label": label, "count": count}

    @staticmethod
    def _gap_duration(summary: str) -> float | None:
        match = re.search(r"gap from (\S+) to (\S+)", summary, re.IGNORECASE)
        if not match:
            return None
        try:
            start = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
            end = datetime.fromisoformat(match.group(2).replace("Z", "+00:00"))
        except ValueError:
            return None
        return max(0, (_utc(end) - _utc(start)).total_seconds())

    def _media_coverage(self, query: ReportQuery) -> dict[str, object]:
        source_statement = (
            select(MediaSource.id, MediaSource.camera_id, Camera.code)
            .join(Camera, Camera.id == MediaSource.camera_id)
            .where(Camera.site_id == query.site.id, MediaSource.recording_enabled.is_(True))
        )
        if query.camera:
            source_statement = source_statement.where(MediaSource.camera_id == query.camera.id)
        sources = self.db.execute(source_statement).all()
        if not sources:
            return {"available": False, "message": "No recording source is configured for this selection."}
        source_ids = [row.id for row in sources]
        segments = self.db.execute(
            select(RecordingSegment.media_source_id, RecordingSegment.started_at, RecordingSegment.ended_at)
            .where(RecordingSegment.media_source_id.in_(source_ids),
                   RecordingSegment.state.in_(("COMPLETE", "PARTIAL")),
                   RecordingSegment.started_at < query.end_at,
                   RecordingSegment.ended_at.is_not(None),
                   RecordingSegment.ended_at > query.start_at)
            .order_by(RecordingSegment.media_source_id, RecordingSegment.started_at)
        ).all()
        if not segments:
            return {"available": False, "message": "No completed recording segments overlap this period."}
        by_source: dict[UUID, list[tuple[datetime, datetime]]] = defaultdict(list)
        for row in segments:
            by_source[row.media_source_id].append((max(_utc(row.started_at), query.start_at), min(_utc(row.ended_at), query.end_at)))
        covered = observed = missing = 0.0
        gap_count = 0
        longest = 0.0
        for intervals in by_source.values():
            merged: list[list[datetime]] = []
            for start, end in intervals:
                if end <= start:
                    continue
                if merged and start <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            if not merged:
                continue
            source_covered = sum((end - start).total_seconds() for start, end in merged)
            source_observed = (merged[-1][1] - merged[0][0]).total_seconds()
            covered += source_covered
            observed += source_observed
            for previous, current in zip(merged, merged[1:]):
                gap = max(0, (current[0] - previous[1]).total_seconds())
                if gap:
                    gap_count += 1
                    missing += gap
                    longest = max(longest, gap)
        if observed <= 0:
            return {"available": False, "message": "Recording coverage could not be calculated from completed segments."}
        return {
            "available": True,
            "percent": round(covered * 100 / observed, 1),
            "covered": _duration(covered), "observed": _duration(observed),
            "gap_count": gap_count, "missing": _duration(missing), "longest_gap": _duration(longest),
            "message": "Coverage is calculated only across the observed span of completed recording segments.",
        }


def export_rows(domain: str, data: dict[str, object]) -> Iterable[tuple[str, str, str, object]]:
    if domain == "traffic":
        yield "Summary", "Entry", "", data["entries"]
        yield "Summary", "Exit", "", data["exits"]
        yield "Summary", "Peak Occupancy", str(data["peak_occupancy_at"]), data["peak_occupancy"]
        yield "Summary", "Average Completed Stay", "", data["average_stay"]
        for row in data["trend"]:
            yield "Traffic Trend", "Entry", str(row["label"]), row["entries"]
            yield "Traffic Trend", "Exit", str(row["label"]), row["exits"]
        for row in data["occupancy"]:
            yield "Occupancy", "Observed Occupancy", str(row["label"]), row["occupancy"]
        for row in data["stay_distribution"]:
            yield "Stay Duration", str(row["label"]), "", row["count"]
    elif domain == "access":
        for key, value in data["counts"].items():
            yield "Decision", _friendly(key), "", value
        for row in data["denial_reasons"]:
            yield "Denial Reason", str(row["label"]), "", row["count"]
        for row in data["profiles"]:
            yield "Access Profile", str(row["label"]), "", row["count"]
        yield "Security", "Blacklist Detections", "", data["blacklist"]
        yield "Security", "Watchlist Detections", "", data["watchlist"]
    elif domain == "operations":
        for label, key in (("Barrier Commands", "barrier_commands"), ("Manual Releases", "manual_releases"),
                           ("Plate Corrections", "corrections"), ("Session Reconciliations", "reconciliations")):
            yield "Summary", label, "", data[key]
        for row in data["outcomes"]:
            yield "Barrier Outcome", str(row["label"]), "", row["count"]
        for key, value in data["recovery"].items():
            yield "Offline Recovery", _friendly(key), "", value
    elif domain == "reliability":
        yield "Summary", "Observed Incidents", "", data["incidents"]
        yield "Summary", "Recording Gap Events", "", data["gap_count"]
        yield "Summary", "Known Gap Duration", "", data["gap_duration"]
        for row in data["incident_types"]:
            yield "Incident Type", str(row["label"]), "", row["count"]
        for row in data["alert_types"]:
            yield "Alert Type", str(row["label"]), "", row["count"]
        coverage = data["coverage"]
        if coverage.get("available"):
            yield "Media Coverage", "Observed Coverage Percent", "", coverage["percent"]
