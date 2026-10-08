from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models.parking import AccessProfile, DecisionResponseProfile, OverstayAlert, ParkingSession, ParkingSessionIssue, SitePolicy
from app.models.site import Site
from app.models.camera import Camera
from app.models.vehicle import Vehicle
from app.models.access_list_entry import AccessListEntry
from app.services.parking_policy_service import AccessStatusResolver, DecisionContext, ResidentialPolicy


class ParkingValidationError(ValueError):
    pass


class ParkingService:
    def __init__(self, db: Session):
        self.db = db

    def sites(self):
        return tuple(self.db.scalars(select(Site).order_by(Site.code)).all())

    def on_site(self, site_id: UUID | None = None):
        query = select(ParkingSession).options(joinedload(ParkingSession.vehicle), joinedload(ParkingSession.site), joinedload(ParkingSession.access_profile)).where(ParkingSession.status.in_(("OPEN", "OVERSTAY"))).order_by(ParkingSession.entered_at.desc())
        if site_id:
            query = query.where(ParkingSession.site_id == site_id)
        return tuple(self._session(item) for item in self.db.scalars(query).unique().all())

    def history(self, site_id: UUID | None = None):
        query = select(ParkingSession).options(joinedload(ParkingSession.vehicle), joinedload(ParkingSession.site), joinedload(ParkingSession.access_profile)).order_by(ParkingSession.entered_at.desc()).limit(500)
        if site_id:
            query = query.where(ParkingSession.site_id == site_id)
        return tuple(self._session(item) for item in self.db.scalars(query).unique().all())

    def get_session(self, session_id: UUID):
        item = self.db.scalar(select(ParkingSession).options(joinedload(ParkingSession.vehicle), joinedload(ParkingSession.site), joinedload(ParkingSession.access_profile)).where(ParkingSession.id == session_id))
        return self._session(item) if item else None

    def alerts(self):
        rows = self.db.scalars(select(OverstayAlert).options(joinedload(OverstayAlert.parking_session).joinedload(ParkingSession.vehicle)).where(OverstayAlert.status.in_(("OPEN", "ACKNOWLEDGED"))).order_by(OverstayAlert.detected_at.desc())).unique().all()
        return tuple({"id": a.id, "plate": a.parking_session.vehicle.plate_text, "status": a.status, "detected": _display(a.detected_at, "Asia/Kuala_Lumpur")} for a in rows)

    def summary(self):
        on_site = self.db.scalar(select(func.count()).select_from(ParkingSession).where(ParkingSession.status.in_(("OPEN", "OVERSTAY")))) or 0
        overstays = self.db.scalar(select(func.count()).select_from(ParkingSession).where(ParkingSession.status == "OVERSTAY")) or 0
        issues = self.db.scalar(select(func.count()).select_from(ParkingSessionIssue).where(ParkingSessionIssue.status == "OPEN")) or 0
        return {"on_site": on_site, "overstays": overstays, "issues": issues}

    def profiles(self, site_id: UUID | None = None):
        query = select(AccessProfile).order_by(AccessProfile.name)
        if site_id:
            query = query.where(AccessProfile.site_id == site_id)
        return tuple(self.db.scalars(query).all())

    def save_profile(self, *, profile_id: UUID | None, site_id: UUID, name: str, description: str, max_stay: str, monitoring: bool, enabled: bool):
        site = self.db.get(Site, site_id)
        if site is None or not name.strip():
            raise ParkingValidationError("Select a site and enter a profile name.")
        try:
            minutes = int(max_stay) if max_stay.strip() else None
        except ValueError as error:
            raise ParkingValidationError("Maximum stay must be whole minutes.") from error
        if minutes is not None and minutes <= 0:
            raise ParkingValidationError("Maximum stay must be greater than zero.")
        profile = self.db.get(AccessProfile, profile_id) if profile_id else AccessProfile(site_id=site_id)
        if profile is None:
            raise ParkingValidationError("Access profile was not found.")
        profile.site_id = site_id
        profile.name = name.strip(); profile.description = description.strip() or None
        profile.default_max_stay_minutes = minutes; profile.overstay_monitoring_enabled = monitoring; profile.enabled = enabled
        self.db.add(profile); self.db.flush(); return profile

    def policy(self, site_id: UUID):
        return self.db.scalar(select(SitePolicy).where(SitePolicy.site_id == site_id))

    def response_profiles(self, site_id: UUID):
        return tuple(self.db.scalars(select(DecisionResponseProfile).where(DecisionResponseProfile.site_id == site_id).order_by(DecisionResponseProfile.name)).all())

    def save_response_profile(self, profile_id: UUID, *, name: str, display_seconds: int, traffic_state: str, green_seconds: int, speaker_enabled: bool, speaker_message: str, rows: list[dict]):
        profile = self.db.get(DecisionResponseProfile, profile_id)
        if profile is None:
            raise ParkingValidationError("Response profile was not found.")
        if not name.strip() or traffic_state not in {"RED", "GREEN"} or not 1 <= display_seconds <= 300 or not 1 <= green_seconds <= 300:
            raise ParkingValidationError("Enter valid response profile settings.")
        profile.name = name.strip(); profile.display_seconds = display_seconds; profile.traffic_state = traffic_state
        profile.traffic_green_seconds = green_seconds; profile.speaker_enabled = speaker_enabled
        profile.speaker_message = speaker_message.strip()[:250] or "{plate}"; profile.led_rows = rows[:4]
        self.db.flush(); return profile

    def save_policy(self, site_id: UUID, *, operating_mode: str, unknown_entry: str, exit_mode: str, exit_blacklist: str, allow_profile: str, deny_profile: str, review_profile: str):
        if operating_mode not in {"RESIDENTIAL", "COMMERCIAL"} or unknown_entry not in {"DENY", "REVIEW"} or exit_mode not in {"FREE_EXIT", "ENFORCED"} or exit_blacklist not in {"ALLOW", "REVIEW", "DENY"}:
            raise ParkingValidationError("Invalid policy selection.")
        policy = self.policy(site_id) or SitePolicy(site_id=site_id)
        profile_ids = {str(p.id) for p in self.response_profiles(site_id)}
        selected = [allow_profile, deny_profile, review_profile]
        if any(value and value not in profile_ids for value in selected):
            raise ParkingValidationError("Select response profiles from this site.")
        policy.operating_mode = operating_mode; policy.unknown_entry_policy = unknown_entry
        policy.exit_mode = exit_mode; policy.exit_blacklist_policy = exit_blacklist
        policy.allow_response_profile_id = UUID(allow_profile) if allow_profile else None
        policy.deny_response_profile_id = UUID(deny_profile) if deny_profile else None
        policy.review_response_profile_id = UUID(review_profile) if review_profile else None
        policy.policy_version = (policy.policy_version or 0) + 1
        self.db.add(policy); self.db.flush(); return policy

    def simulate(self, *, site_id: UUID, camera_id: UUID, plate: str, evaluated_at: datetime):
        site = self.db.get(Site, site_id); camera = self.db.get(Camera, camera_id)
        if site is None or camera is None or camera.site_id != site.id:
            raise ParkingValidationError("Select a camera at the selected site.")
        normalized = "".join(char for char in plate.strip().upper() if char.isalnum())
        if not normalized:
            raise ParkingValidationError("Enter a sample plate.")
        vehicle = self.db.scalar(select(Vehicle).where(Vehicle.plate_text == normalized))
        entry = self.db.scalar(select(AccessListEntry).where(AccessListEntry.vehicle_id == vehicle.id, AccessListEntry.site_id == site.id)) if vehicle else None
        policy = self.policy(site.id)
        status = AccessStatusResolver.resolve(entry, at=evaluated_at, timezone_name=site.timezone)
        event = type("SimulationEvent", (), {"source": "LIVE"})()
        context = DecisionContext(event, camera, site, vehicle, entry, status, policy, evaluated_at)
        result = ResidentialPolicy.evaluate(context)
        profile = self.db.get(AccessProfile, entry.access_profile_id) if entry and entry.access_profile_id else None
        maximum = entry.max_stay_minutes_override if entry and entry.max_stay_minutes_override is not None else (profile.default_max_stay_minutes if profile and profile.enabled else None)
        return {"plate": normalized, "lane": camera.lane_role.title(), "access_status": status.replace("_", " ").title(), "decision": {"ALLOW": "Granted", "DENY": "Denied", "REVIEW": "Review Required", "NO_ACTION": "No Action"}[result.decision], "reason": result.reason_code.replace("_", " ").title(), "stay_rule": f"{maximum} minutes" if maximum else "Unlimited"}

    @staticmethod
    def _session(item):
        tz = item.site.timezone
        now = datetime.now(timezone.utc)
        end = item.exited_at or now
        entered = item.entered_at.replace(tzinfo=timezone.utc) if item.entered_at.tzinfo is None else item.entered_at
        ended = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end
        duration = max(0, int((ended - entered).total_seconds() // 60))
        return {"id": item.id, "plate": item.vehicle.plate_text, "name": item.vehicle.reference_name,
            "site": item.site.code, "status": item.status, "entered": _display(item.entered_at, tz),
            "exited": _display(item.exited_at, tz) if item.exited_at else None, "duration": f"{duration // 60}h {duration % 60}m",
            "profile": item.access_profile.name if item.access_profile else "No profile", "max_stay": item.max_stay_minutes,
            "overstay_due": _display(item.overstay_due_at, tz) if item.overstay_due_at else None, "source": item.source}


def _display(value, timezone_name):
    if value is None: return None
    try: zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError: zone = timezone.utc
    value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return value.astimezone(zone).strftime("%d %b %Y %H:%M:%S")
