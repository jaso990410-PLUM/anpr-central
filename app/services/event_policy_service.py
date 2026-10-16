from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EventPolicy:
    category: str
    severity: str = "INFO"
    attention_required: bool = False
    audit_significant: bool = False


_INFO_TRAFFIC = EventPolicy("TRAFFIC_ANPR")
EVENT_POLICIES: dict[str, EventPolicy] = {
    "ANPR_RECOGNITION": _INFO_TRAFFIC,
    "OFFLINE_RECOGNITION_RECOVERED": EventPolicy("TRAFFIC_ANPR"),
    "ACCESS_ALLOWED": _INFO_TRAFFIC,
    "ACCESS_DENIED": EventPolicy("ACCESS_SECURITY", "WARNING", True),
    "ACCESS_REVIEW": EventPolicy("ACCESS_SECURITY", "WARNING", True),
    "WHITELIST_MATCH": EventPolicy("ACCESS_SECURITY"),
    "BLACKLIST_MATCH": EventPolicy("ACCESS_SECURITY", "WARNING", True),
    "WATCHLIST_MATCH": EventPolicy("ACCESS_SECURITY", "WARNING", True),
    "VEHICLE_ENTRY": _INFO_TRAFFIC,
    "VEHICLE_EXIT": _INFO_TRAFFIC,
    "PARKING_SESSION_OPENED": _INFO_TRAFFIC,
    "PARKING_SESSION_CLOSED": _INFO_TRAFFIC,
    "PLATE_CORRECTED": EventPolicy("OPERATOR_ACTION", audit_significant=True),
    "SESSION_RECONCILED": EventPolicy("OPERATOR_ACTION", "WARNING", True, True),
    "MANUAL_BARRIER_RELEASE_REQUESTED": EventPolicy("OPERATOR_ACTION", "INFO", True, True),
    "MANUAL_BARRIER_RELEASE_COMPLETED": EventPolicy("OPERATOR_ACTION", "INFO", True, True),
    "BARRIER_COMMAND_FAILED": EventPolicy("SYSTEM_HEALTH", "WARNING", True, True),
    "VISITOR_REGISTERED": EventPolicy("ADMIN_BUSINESS", audit_significant=True),
    "VISITOR_UPDATED": EventPolicy("ADMIN_BUSINESS", audit_significant=True),
    "VISITOR_CANCELLED": EventPolicy("ADMIN_BUSINESS", audit_significant=True),
    "ACCESS_ENTRY_CREATED": EventPolicy("ADMIN_BUSINESS", audit_significant=True),
    "ACCESS_ENTRY_UPDATED": EventPolicy("ADMIN_BUSINESS", audit_significant=True),
    "ACCESS_ENTRY_REVOKED": EventPolicy("ADMIN_BUSINESS", audit_significant=True),
    "BLACKLIST_ENTRY_CREATED": EventPolicy("ADMIN_BUSINESS", "INFO", True, True),
    "BLACKLIST_ENTRY_REMOVED": EventPolicy("ADMIN_BUSINESS", "INFO", True, True),
    "ACCESS_PROFILE_CREATED": EventPolicy("ADMIN_BUSINESS", audit_significant=True),
    "ACCESS_PROFILE_UPDATED": EventPolicy("ADMIN_BUSINESS", audit_significant=True),
    "ALERT_ACKNOWLEDGED": EventPolicy("OPERATOR_ACTION", audit_significant=True),
    "CAMERA_OFFLINE": EventPolicy("SYSTEM_HEALTH", "WARNING", True),
    "CAMERA_ONLINE": EventPolicy("SYSTEM_HEALTH"),
    "MQTT_DISCONNECTED": EventPolicy("SYSTEM_HEALTH", "CRITICAL", True),
    "MQTT_RECONNECTED": EventPolicy("SYSTEM_HEALTH"),
    "DATABASE_UNAVAILABLE": EventPolicy("SYSTEM_HEALTH", "CRITICAL", True),
    "DATABASE_RECOVERED": EventPolicy("SYSTEM_HEALTH"),
    "DATABASE_SLOW": EventPolicy("SYSTEM_HEALTH", "WARNING", True),
    "RECORDER_STOPPED": EventPolicy("SYSTEM_HEALTH", "CRITICAL", True),
    "RECORDER_STARTED": EventPolicy("SYSTEM_HEALTH"),
    "RECORDING_STALE": EventPolicy("SYSTEM_HEALTH", "WARNING", True),
    "RECORDING_RESUMED": EventPolicy("SYSTEM_HEALTH"),
    "RECORDING_GAP": EventPolicy("SYSTEM_HEALTH", "WARNING", True),
    "STORAGE_LOW": EventPolicy("SYSTEM_HEALTH", "WARNING", True),
    "STORAGE_CRITICAL": EventPolicy("SYSTEM_HEALTH", "CRITICAL", True),
    "STORAGE_UNAVAILABLE": EventPolicy("SYSTEM_HEALTH", "CRITICAL", True),
    "STORAGE_RECOVERED": EventPolicy("SYSTEM_HEALTH"),
    "ACCESS_SYNC_MISMATCH": EventPolicy("SYSTEM_HEALTH", "WARNING", True),
    "ACCESS_SYNC_RESTORED": EventPolicy("SYSTEM_HEALTH"),
    "OFFLINE_BACKLOG": EventPolicy("SYSTEM_HEALTH", "WARNING", True),
    "OFFLINE_BACKLOG_CLEARED": EventPolicy("SYSTEM_HEALTH"),
    "BARRIER_CONTROL_DEGRADED": EventPolicy("SYSTEM_HEALTH", "WARNING", True),
    "BARRIER_CONTROL_RESTORED": EventPolicy("SYSTEM_HEALTH"),
    "PERIPHERAL_COMMAND_FAILED": EventPolicy("SYSTEM_HEALTH", "WARNING", True),
    "COMPONENT_FAULT": EventPolicy("SYSTEM_HEALTH", "WARNING", True),
    "COMPONENT_RECOVERED": EventPolicy("SYSTEM_HEALTH"),
}


def event_policy(event_type: str) -> EventPolicy:
    try:
        return EVENT_POLICIES[event_type]
    except KeyError as error:
        raise ValueError(f"Unsupported operational event type: {event_type}") from error
