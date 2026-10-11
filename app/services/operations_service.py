from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.access_list_entry import AccessListEntry
from app.models.barrier import BarrierAction, BarrierState
from app.models.camera import Camera
from app.models.operations import ParkingReconciliation, RecognitionPlateCorrection, VehicleWatch
from app.models.parking import AccessDecision, ParkingReviewItem, ParkingSession, ParkingSessionIssue
from app.models.recognition_event import RecognitionEvent
from app.models.site import Site
from app.models.vehicle import Vehicle
from app.services.access_list_service import AccessListValidationError, normalize_plate
from app.services.parking_service import ParkingService, ParkingValidationError
from app.services.plate_correction_service import PlateCorrectionService, effective_plate


@dataclass(frozen=True)
class SimilarPlate:
    plate: str
    score: int
    sources: tuple[str, ...]
    reference_name: str | None = None


class OperationsService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def sites(self) -> tuple[Site, ...]:
        return tuple(self.db.scalars(select(Site).where(Site.is_active.is_(True)).order_by(Site.code)).all())

    def cameras(self, site_id: UUID | None = None) -> tuple[Camera, ...]:
        query = select(Camera).where(Camera.is_enabled.is_(True)).order_by(Camera.code)
        if site_id:
            query = query.where(Camera.site_id == site_id)
        return tuple(self.db.scalars(query).all())

    def recent(
        self,
        *,
        site_id: UUID | None = None,
        camera_id: UUID | None = None,
        lane_role: str = "",
        decision: str = "",
        exceptions_only: bool = False,
        query_text: str = "",
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 50,
    ) -> tuple[dict[str, object], ...]:
        query = (
            select(RecognitionEvent, Camera, Site, AccessDecision, ParkingSessionIssue, VehicleWatch)
            .join(Camera, Camera.id == RecognitionEvent.camera_id)
            .join(Site, Site.id == RecognitionEvent.site_id)
            .outerjoin(AccessDecision, AccessDecision.recognition_event_id == RecognitionEvent.id)
            .outerjoin(ParkingSessionIssue, ParkingSessionIssue.recognition_event_id == RecognitionEvent.id)
            .outerjoin(VehicleWatch, (VehicleWatch.site_id == RecognitionEvent.site_id) & (VehicleWatch.plate_normalized == RecognitionEvent.plate_text) & VehicleWatch.active.is_(True))
        )
        conditions = []
        if site_id:
            conditions.append(RecognitionEvent.site_id == site_id)
        if camera_id:
            conditions.append(RecognitionEvent.camera_id == camera_id)
        if lane_role in {"ENTRY", "EXIT"}:
            conditions.append(Camera.lane_role == lane_role)
        if decision in {"ALLOW", "REVIEW", "DENY", "NO_ACTION"}:
            conditions.append(AccessDecision.decision == decision)
        if exceptions_only:
            conditions.append(or_(ParkingSessionIssue.status == "OPEN", AccessDecision.decision.in_(("REVIEW", "DENY"))))
        display_zone = self._site_timezone(site_id)
        captured_time = func.coalesce(RecognitionEvent.captured_at, RecognitionEvent.received_at)
        if date_from:
            conditions.append(captured_time >= datetime.combine(date_from, time.min, display_zone).astimezone(timezone.utc))
        if date_to:
            conditions.append(captured_time < datetime.combine(date_to + timedelta(days=1), time.min, display_zone).astimezone(timezone.utc))
        clean_query = query_text.strip()
        if clean_query:
            conditions.append(or_(
                RecognitionEvent.plate_text.ilike(f"%{clean_query}%"),
                RecognitionEvent.id.in_(select(RecognitionPlateCorrection.recognition_event_id).where(RecognitionPlateCorrection.corrected_plate_normalized.ilike(f"%{clean_query.upper()}%"))),
                RecognitionEvent.plate_text.in_(select(Vehicle.plate_text).where(Vehicle.reference_name.ilike(f"%{clean_query}%"))),
            ))
        rows = self.db.execute(
            query.where(*conditions)
            .order_by(func.coalesce(RecognitionEvent.captured_at, RecognitionEvent.received_at).desc(), RecognitionEvent.id.desc())
            .limit(min(max(1, limit), 100))
        ).all()
        return tuple(self._recent_row(*row) for row in rows)

    def _site_timezone(self, site_id: UUID | None):
        if site_id:
            site = self.db.get(Site, site_id)
            if site:
                try:
                    return ZoneInfo(site.timezone)
                except ZoneInfoNotFoundError:
                    pass
        try:
            return ZoneInfo("Asia/Kuala_Lumpur")
        except ZoneInfoNotFoundError:
            return timezone.utc

    def detail(self, event_id: UUID) -> dict[str, object] | None:
        event = self.db.scalar(select(RecognitionEvent).options(joinedload(RecognitionEvent.camera), joinedload(RecognitionEvent.site)).where(RecognitionEvent.id == event_id))
        if event is None:
            return None
        plate = effective_plate(self.db, event)
        correction_service = PlateCorrectionService(self.db)
        corrections = correction_service.history(event.id)
        decision = self.db.scalar(select(AccessDecision).where(AccessDecision.recognition_event_id == event.id))
        vehicle = self.db.scalar(select(Vehicle).where(Vehicle.plate_text == plate))
        access = self.db.scalar(select(AccessListEntry).where(AccessListEntry.site_id == event.site_id, AccessListEntry.vehicle_id == vehicle.id)) if vehicle else None
        session = self.db.scalar(select(ParkingSession).where(or_(ParkingSession.entry_recognition_id == event.id, ParkingSession.exit_recognition_id == event.id)))
        if session is None and vehicle is not None:
            session = self.db.scalar(select(ParkingSession).where(ParkingSession.site_id == event.site_id, ParkingSession.vehicle_id == vehicle.id, ParkingSession.status.in_(("OPEN", "OVERSTAY"))).order_by(ParkingSession.entered_at.desc()))
        issue = self.db.scalar(select(ParkingSessionIssue).where(ParkingSessionIssue.recognition_event_id == event.id))
        review = self.db.scalar(select(ParkingReviewItem).where(ParkingReviewItem.recognition_event_id == event.id).order_by(ParkingReviewItem.last_triggered_at.desc()))
        watch = self.active_watch(event.site_id, plate)
        barrier_action = self.db.scalar(select(BarrierAction).where(BarrierAction.recognition_event_id == event.id).order_by(BarrierAction.requested_at.desc()).limit(1))
        barrier_state = self.db.scalar(select(BarrierState).where(BarrierState.camera_id == event.camera_id))
        reconciliations = tuple(self.db.scalars(select(ParkingReconciliation).where(or_(ParkingReconciliation.primary_recognition_event_id == event.id, ParkingReconciliation.secondary_recognition_event_id == event.id)).order_by(ParkingReconciliation.performed_at.desc())).all())
        try:
            preview = ParkingService(self.db).simulate(site_id=event.site_id, camera_id=event.camera_id, plate=plate, evaluated_at=datetime.now(timezone.utc))
        except ParkingValidationError:
            preview = None
        return {
            "event": event,
            "raw_plate": event.plate_text,
            "effective_plate": plate,
            "latest_correction": corrections[0] if corrections else None,
            "corrections": corrections,
            "decision": decision,
            "access": access,
            "vehicle": vehicle,
            "session": session,
            "issue": issue,
            "review": review,
            "watch": watch,
            "barrier_action": barrier_action,
            "barrier_state": barrier_state,
            "reconciliations": reconciliations,
            "preview": preview,
            "captured_display": self._display(event.captured_at or event.received_at, event.site.timezone),
            "captured_iso": _utc(event.captured_at or event.received_at).isoformat(),
            "scene_url": f"/recognition/{event.id}/image/scene" if event.scene_image_path else None,
            "plate_url": f"/recognition/{event.id}/image/plate" if event.plate_image_path else None,
        }

    def open_session_candidates(self, event: RecognitionEvent, *, limit: int = 20) -> tuple[dict[str, object], ...]:
        plate = effective_plate(self.db, event)
        sessions = self.db.scalars(
            select(ParkingSession)
            .options(joinedload(ParkingSession.vehicle), joinedload(ParkingSession.entry_recognition).joinedload(RecognitionEvent.camera))
            .where(ParkingSession.site_id == event.site_id, ParkingSession.status.in_(("OPEN", "OVERSTAY")))
            .order_by(ParkingSession.entered_at.desc())
            .limit(100)
        ).unique().all()
        ranked = sorted(sessions, key=lambda session: (_weighted_distance(plate, session.vehicle.plate_text), -_utc(session.entered_at).timestamp()))[:limit]
        return tuple({
            "session": session,
            "plate": session.vehicle.plate_text,
            "similarity": _similarity_label(plate, session.vehicle.plate_text),
            "entry_camera": session.entry_recognition.camera.name,
            "entered": self._display(session.entered_at, event.site.timezone),
        } for session in ranked)

    def similar_plates(self, plate: str, *, site_id: UUID | None = None, limit: int = 12) -> tuple[SimilarPlate, ...]:
        try:
            normalized = normalize_plate(plate)
        except AccessListValidationError:
            return ()
        candidates: dict[str, dict[str, object]] = {}
        vehicle_query = select(Vehicle)
        if site_id:
            vehicle_query = vehicle_query.join(AccessListEntry).where(AccessListEntry.site_id == site_id)
        for vehicle in self.db.scalars(vehicle_query.limit(1000)).unique().all():
            bucket = candidates.setdefault(vehicle.plate_text, {"sources": set(), "name": vehicle.reference_name})
            if vehicle.access_entries:
                bucket["sources"].add("Access List")
        session_query = select(ParkingSession, Vehicle).join(Vehicle, Vehicle.id == ParkingSession.vehicle_id)
        if site_id:
            session_query = session_query.where(ParkingSession.site_id == site_id)
        for session, vehicle in self.db.execute(session_query.order_by(ParkingSession.entered_at.desc()).limit(500)).all():
            bucket = candidates.setdefault(vehicle.plate_text, {"sources": set(), "name": vehicle.reference_name})
            bucket["sources"].add("Open Session" if session.status in {"OPEN", "OVERSTAY"} else "Historical Session")
        event_query = select(RecognitionEvent.plate_text)
        if site_id:
            event_query = event_query.where(RecognitionEvent.site_id == site_id)
        for candidate in self.db.scalars(event_query.order_by(RecognitionEvent.received_at.desc()).limit(500)).all():
            try:
                candidate = normalize_plate(candidate)
            except AccessListValidationError:
                continue
            candidates.setdefault(candidate, {"sources": set(), "name": None})["sources"].add("Recognition")
        scored = []
        threshold = max(2, len(normalized) // 3)
        for candidate, metadata in candidates.items():
            if abs(len(candidate) - len(normalized)) > 2:
                continue
            score = _weighted_distance(normalized, candidate)
            if score <= threshold:
                scored.append(SimilarPlate(candidate, score, tuple(sorted(metadata["sources"])), metadata["name"]))
        return tuple(sorted(scored, key=lambda item: (item.score, item.plate))[:limit])

    def previous_visits(self, event: RecognitionEvent, *, limit: int = 30) -> tuple[dict[str, object], ...]:
        plate = effective_plate(self.db, event)
        rows = self.db.scalars(select(RecognitionEvent).where(
            RecognitionEvent.site_id == event.site_id,
            or_(
                RecognitionEvent.plate_text == plate,
                RecognitionEvent.id.in_(select(RecognitionPlateCorrection.recognition_event_id).where(RecognitionPlateCorrection.corrected_plate_normalized == plate)),
            ),
        ).order_by(func.coalesce(RecognitionEvent.captured_at, RecognitionEvent.received_at).desc()).limit(limit)).all()
        result = []
        for item in rows:
            decision = self.db.scalar(select(AccessDecision).where(AccessDecision.recognition_event_id == item.id))
            session = self.db.scalar(select(ParkingSession).where(or_(ParkingSession.entry_recognition_id == item.id, ParkingSession.exit_recognition_id == item.id)))
            issue = self.db.scalar(select(ParkingSessionIssue).where(ParkingSessionIssue.recognition_event_id == item.id))
            correction = PlateCorrectionService(self.db).latest(item.id)
            barrier = self.db.scalar(select(BarrierAction).where(BarrierAction.recognition_event_id == item.id).order_by(BarrierAction.requested_at.desc()).limit(1))
            result.append({
                "event": item,
                "captured": self._display(item.captured_at or item.received_at, event.site.timezone),
                "effective_plate": effective_plate(self.db, item),
                "decision": decision.decision if decision else "Pending",
                "session": session.status if session else "None",
                "exception": issue.issue_type.replace("_", " ").title() if issue else "None",
                "correction": correction.corrected_plate_normalized if correction else None,
                "barrier": barrier.trigger_source.replace("_", " ").title() if barrier else "None",
            })
        return tuple(result)

    def active_watch(self, site_id: UUID, plate: str) -> VehicleWatch | None:
        return self.db.scalar(select(VehicleWatch).where(VehicleWatch.site_id == site_id, VehicleWatch.plate_normalized == plate, VehicleWatch.active.is_(True)))

    def watches(self) -> tuple[VehicleWatch, ...]:
        return tuple(self.db.scalars(select(VehicleWatch).options(joinedload(VehicleWatch.site), joinedload(VehicleWatch.created_by), joinedload(VehicleWatch.cleared_by)).order_by(VehicleWatch.active.desc(), VehicleWatch.created_at.desc())).unique().all())

    def add_watch(self, *, site_id: UUID, plate: str, severity: str, note: str, user_id: UUID) -> VehicleWatch:
        site = self.db.get(Site, site_id)
        if site is None:
            raise ValueError("Select a valid site.")
        normalized = normalize_plate(plate)
        if severity not in {"WATCH", "HIGH_ATTENTION"}:
            raise ValueError("Select a valid watch severity.")
        clean_note = note.strip()
        if len(clean_note) < 3:
            raise ValueError("Enter a meaningful watch note.")
        existing = self.active_watch(site_id, normalized)
        if existing is not None:
            raise ValueError("This plate already has an active watch flag at the selected site.")
        watch = VehicleWatch(site_id=site_id, plate_normalized=normalized, severity=severity, note=clean_note[:2000], created_by_user_id=user_id)
        self.db.add(watch)
        self.db.flush()
        return watch

    def clear_watch(self, watch_id: UUID, *, user_id: UUID) -> VehicleWatch:
        watch = self.db.scalar(select(VehicleWatch).where(VehicleWatch.id == watch_id).with_for_update())
        if watch is None:
            raise ValueError("Watch flag was not found.")
        if not watch.active:
            return watch
        watch.active = False
        watch.cleared_by_user_id = user_id
        watch.cleared_at = datetime.now(timezone.utc)
        return watch

    def _recent_row(self, event: RecognitionEvent, camera: Camera, site: Site, decision: AccessDecision | None, issue: ParkingSessionIssue | None, watch: VehicleWatch | None) -> dict[str, object]:
        plate = effective_plate(self.db, event)
        watch = watch or self.active_watch(event.site_id, plate)
        return {
            "id": event.id,
            "raw_plate": event.plate_text,
            "plate": plate,
            "corrected": plate != event.plate_text,
            "camera": camera.name,
            "camera_code": camera.code,
            "camera_state": camera.connection_state or "UNKNOWN",
            "lane_role": camera.lane_role,
            "site": site.code,
            "decision": decision.decision if decision else "PENDING",
            "session": issue.issue_type.replace("_", " ").title() if issue and issue.status == "OPEN" else "Normal",
            "exception": issue.issue_type.replace("_", " ").title() if issue and issue.status == "OPEN" else None,
            "watch": watch.severity.replace("_", " ").title() if watch else None,
            "captured": self._display(event.captured_at or event.received_at, site.timezone),
            "captured_iso": _utc(event.captured_at or event.received_at).isoformat(),
        }

    @staticmethod
    def _display(value: datetime, timezone_name: str) -> str:
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            zone = timezone.utc
        return _utc(value).astimezone(zone).strftime("%d %b %Y %H:%M:%S")


_CONFUSABLE = {frozenset(pair) for pair in (("0", "O"), ("0", "Q"), ("O", "Q"), ("1", "I"), ("5", "S"), ("6", "G"), ("8", "B"), ("2", "Z"))}


def _weighted_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for index, left_char in enumerate(left, start=1):
        current = [index]
        for column, right_char in enumerate(right, start=1):
            substitution = 0 if left_char == right_char else (1 if frozenset((left_char, right_char)) in _CONFUSABLE else 2)
            current.append(min(current[-1] + 2, previous[column] + 2, previous[column - 1] + substitution))
        previous = current
    return previous[-1]


def _similarity_label(left: str, right: str) -> str:
    score = _weighted_distance(left, right)
    return "Exact" if score == 0 else ("Close" if score <= 2 else "Possible")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
