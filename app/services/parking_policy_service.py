from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.access_list_entry import AccessListEntry
from app.models.camera import Camera
from app.models.parking import AccessDecision, AccessProfile, ParkingSession, SitePolicy
from app.models.recognition_event import RecognitionEvent
from app.models.site import Site
from app.models.vehicle import Vehicle


ACCESS_STATUSES = {
    "WHITELIST_ACTIVE", "BLACKLIST_ACTIVE", "WHITELIST_DISABLED", "BLACKLIST_DISABLED",
    "WHITELIST_NOT_YET_VALID", "BLACKLIST_NOT_YET_VALID", "WHITELIST_EXPIRED",
    "BLACKLIST_EXPIRED", "WHITELIST_OUTSIDE_WINDOW", "BLACKLIST_OUTSIDE_WINDOW", "NOT_LISTED",
}


@dataclass(frozen=True)
class DecisionContext:
    event: RecognitionEvent
    camera: Camera
    site: Site
    vehicle: Vehicle | None
    access_entry: AccessListEntry | None
    access_status: str
    policy: SitePolicy | None
    evaluated_at: datetime
    active_session: ParkingSession | None = None
    current_profile: AccessProfile | None = None


@dataclass(frozen=True)
class PolicyResult:
    decision: str
    reason_code: str
    response_profile_id: UUID | None
    exit_policy_source: str | None = None
    barrier_intent: str = "NONE"
    exit_condition: str | None = None


class AccessStatusResolver:
    @staticmethod
    def resolve(entry: AccessListEntry | None, *, at: datetime, timezone_name: str) -> str:
        if entry is None:
            return "NOT_LISTED"
        prefix = "WHITELIST" if entry.list_type == "ALLOW" else "BLACKLIST"
        utc_at = _as_utc(at)
        if not entry.enabled:
            return f"{prefix}_DISABLED"
        if entry.valid_from is not None and utc_at < _as_utc(entry.valid_from):
            return f"{prefix}_NOT_YET_VALID"
        if entry.valid_until is not None and utc_at > _as_utc(entry.valid_until):
            return f"{prefix}_EXPIRED"
        if entry.daily_window_enabled and entry.daily_start is not None and entry.daily_end is not None:
            local_time = utc_at.astimezone(_zone(timezone_name)).time().replace(tzinfo=None)
            if not _inside_window(local_time, entry.daily_start, entry.daily_end):
                return f"{prefix}_OUTSIDE_WINDOW"
        return f"{prefix}_ACTIVE"


class ResidentialPolicy:
    @staticmethod
    def evaluate(context: DecisionContext) -> PolicyResult:
        policy = context.policy
        if context.camera.lane_role not in {"ENTRY", "EXIT"}:
            return PolicyResult("NO_ACTION", "LANE_ROLE_UNSPECIFIED", None)
        if policy is not None and policy.operating_mode != "RESIDENTIAL":
            return PolicyResult("NO_ACTION", "OPERATING_MODE_UNSUPPORTED", None)

        status = context.access_status
        lane = context.camera.lane_role
        unknown_entry = policy.unknown_entry_policy if policy else "DENY"
        exit_mode = policy.exit_mode if policy else "FREE_EXIT"
        exit_blacklist = policy.exit_blacklist_policy if policy else "DENY"

        exit_source = None
        exit_condition = None
        if lane == "EXIT":
            decision, reason, exit_source, exit_condition = ProfileExitPolicyResolver.evaluate(context)
        elif status == "WHITELIST_ACTIVE":
            decision, reason = "ALLOW", "ACTIVE_WHITELIST"
        elif status == "BLACKLIST_ACTIVE":
            decision, reason = "DENY", "ACTIVE_BLACKLIST"
        elif status.startswith("WHITELIST_"):
            decision, reason = "DENY", status
        else:
            decision, reason = unknown_entry, "UNKNOWN_OR_INACTIVE_ACCESS"

        profile_id = None
        if policy:
            profile_id = {
                "ALLOW": policy.allow_response_profile_id,
                "DENY": policy.deny_response_profile_id,
                "REVIEW": policy.review_response_profile_id,
            }.get(decision)
        intent = {"ALLOW": "OPEN", "DENY": "HOLD", "REVIEW": "OPERATOR_REVIEW"}.get(decision, "NONE")
        return PolicyResult(decision, reason, profile_id, exit_source, intent, exit_condition)


class ProfileExitPolicyResolver:
    """Resolve exit policy from the entry-time snapshot before current configuration."""

    @staticmethod
    def evaluate(context: DecisionContext) -> tuple[str, str, str, str]:
        status = context.access_status
        session = context.active_session
        profile = context.current_profile

        if status == "BLACKLIST_ACTIVE":
            condition = "BLACKLIST"
        elif session is not None and session.overstay_due_at is not None:
            enforcement_at = _as_utc(session.overstay_due_at) + timedelta(
                minutes=session.effective_overstay_exit_grace_minutes
            )
            condition = "OVERSTAY" if context.evaluated_at > enforcement_at else (
                "EXPIRED" if status == "WHITELIST_EXPIRED" else "NORMAL"
            )
        elif status == "WHITELIST_EXPIRED":
            condition = "EXPIRED"
        else:
            condition = "NORMAL"

        field = {
            "NORMAL": "normal_exit_policy",
            "EXPIRED": "expired_exit_policy",
            "OVERSTAY": "overstay_exit_policy",
            "BLACKLIST": "blacklist_exit_policy",
        }[condition]
        if session is not None:
            rule = getattr(session, f"effective_{field}", "USE_SITE_POLICY")
            source = "SESSION_PROFILE_SNAPSHOT"
        elif profile is not None and profile.enabled:
            rule = getattr(profile, field, "USE_SITE_POLICY")
            source = "CURRENT_ACCESS_PROFILE"
        else:
            rule = "USE_SITE_POLICY"
            source = "SITE_POLICY_FALLBACK"

        if rule in {"ALLOW", "REVIEW", "DENY"}:
            return rule, f"PROFILE_{condition}_EXIT_{rule}", source, condition
        decision, reason = ProfileExitPolicyResolver._site_fallback(context)
        return decision, reason, "SITE_POLICY_FALLBACK", condition

    @staticmethod
    def _site_fallback(context: DecisionContext) -> tuple[str, str]:
        policy = context.policy
        if policy is None:
            return "REVIEW", "EXIT_POLICY_UNAVAILABLE"
        if policy.exit_mode == "FREE_EXIT":
            if context.access_status == "BLACKLIST_ACTIVE":
                decision = policy.exit_blacklist_policy
                return decision, f"EXIT_BLACKLIST_{decision}"
            return "ALLOW", "FREE_EXIT"
        if context.access_status == "WHITELIST_ACTIVE":
            return "ALLOW", "ACTIVE_WHITELIST"
        if context.access_status == "BLACKLIST_ACTIVE":
            return "DENY", "ACTIVE_BLACKLIST"
        if context.access_status.startswith("WHITELIST_"):
            return "DENY", context.access_status
        return policy.unknown_entry_policy, "UNKNOWN_OR_INACTIVE_ACCESS"


class DecisionContextBuilder:
    def __init__(self, db: Session):
        self.db = db

    def build(self, event: RecognitionEvent) -> DecisionContext:
        camera = self.db.get(Camera, event.camera_id)
        site = self.db.get(Site, event.site_id)
        if camera is None or site is None:
            raise ValueError("Recognition camera/site is unavailable")
        vehicle = self.db.scalar(select(Vehicle).where(Vehicle.plate_text == event.plate_text))
        entry = None
        if vehicle is not None:
            entry = self.db.scalar(select(AccessListEntry).where(
                AccessListEntry.vehicle_id == vehicle.id, AccessListEntry.site_id == site.id
            ))
        evaluated_at = _as_utc(event.captured_at or event.received_at)
        status = AccessStatusResolver.resolve(entry, at=evaluated_at, timezone_name=site.timezone)
        policy = self.db.scalar(select(SitePolicy).where(SitePolicy.site_id == site.id))
        active = None
        profile = None
        if vehicle is not None:
            active = self.db.scalar(select(ParkingSession).where(
                ParkingSession.site_id == site.id, ParkingSession.vehicle_id == vehicle.id,
                ParkingSession.status.in_(("OPEN", "OVERSTAY")),
            ))
        if entry is not None and entry.access_profile_id is not None:
            profile = self.db.get(AccessProfile, entry.access_profile_id)
        return DecisionContext(event, camera, site, vehicle, entry, status, policy, evaluated_at, active, profile)


def create_access_decision(db: Session, context: DecisionContext) -> AccessDecision:
    existing = db.scalar(select(AccessDecision).where(AccessDecision.recognition_event_id == context.event.id))
    if existing is not None:
        return existing
    if context.event.source == "OFFLINE_RECOVERY":
        result = PolicyResult("NO_ACTION", "HISTORICAL_RECONCILIATION", None, None, "NONE", None)
        response_status = "NOT_APPLICABLE"
    else:
        result = ResidentialPolicy.evaluate(context)
        response_status = "PENDING" if result.decision != "NO_ACTION" and result.response_profile_id else "NOT_CONFIGURED"
    decision = AccessDecision(
        recognition_event_id=context.event.id, site_id=context.site.id, camera_id=context.camera.id,
        vehicle_id=context.vehicle.id if context.vehicle else None,
        access_entry_id=context.access_entry.id if context.access_entry else None,
        access_status=context.access_status, decision=result.decision, reason_code=result.reason_code,
        policy_type=context.policy.operating_mode if context.policy else "RESIDENTIAL",
        policy_version=context.policy.policy_version if context.policy else 1,
        evaluated_at=context.evaluated_at, response_profile_id=result.response_profile_id,
        response_status=response_status, exit_policy_source=result.exit_policy_source,
        barrier_intent=result.barrier_intent, physical_execution="NOT_IMPLEMENTED",
    )
    db.add(decision)
    db.flush()
    return decision


def _inside_window(value: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= value <= end
    return value >= start or value <= end


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone.utc
