from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog


_SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "authorization",
    "cookie",
    "credential",
)


def _safe_details(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _safe_details(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in _SENSITIVE_KEY_PARTS)
        }
    if isinstance(value, (list, tuple)):
        return [_safe_details(item) for item in value]
    return value


def create_audit_log(
    db: Session,
    *,
    action: str,
    actor_user_id: UUID | None = None,
    site_id: UUID | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    details: Mapping[str, Any] | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    """Stage an audit event, removing credential-like detail fields."""
    audit_log = AuditLog(
        actor_user_id=actor_user_id,
        site_id=site_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=_safe_details(details) if details is not None else None,
        ip_address=ip_address,
    )
    db.add(audit_log)
    db.flush()
    return audit_log
