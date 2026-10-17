from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BeforeValidator
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.db.dependencies import get_db
from app.dependencies.auth import require_page_permission, require_permission, user_has_permission
from app.dependencies.csrf import require_session_csrf
from app.models.monitoring import ComponentHealth, OperationalAlert, UnifiedEvent
from app.models.user import User
from app.services.alert_service import AlertService
from app.services.dashboard_service import get_dashboard_summary
from app.services.monitoring_query_service import MonitoringQueryService
from app.services.shell_service import build_navigation, role_label
from app.web import templates


router = APIRouter()


def _blank(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


OptionalUUID = Annotated[UUID | None, BeforeValidator(_blank), Query()]
OptionalDateTime = Annotated[datetime | None, BeforeValidator(_blank), Query()]


def _context(request: Request, db: Session, user: User, active_page: str) -> dict[str, object]:
    return {
        "request": request, "user": user,
        "csrf_token": generate_csrf_token(request.cookies[settings.session_cookie_name]),
        "summary": get_dashboard_summary(db), "active_page": active_page,
        "navigation": build_navigation(user), "user_role_label": role_label(user),
        "can_acknowledge": user_has_permission(user, "alerts.acknowledge"),
    }


@router.get("/events", response_class=HTMLResponse)
def event_center(
    request: Request, tab: str = "attention", page: int = Query(1, ge=1),
    category: str = "", event_type: str = "", severity: str = "",
    site: OptionalUUID = None, camera: OptionalUUID = None, search: str = "", operator: str = "",
    date_from: OptionalDateTime = None, date_to: OptionalDateTime = None,
    user: User = Depends(require_page_permission("monitoring.view")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if tab not in {"attention", "alerts", "all", "history"}:
        tab = "attention"
    if date_from and date_to:
        local_zone = ZoneInfo("Asia/Kuala_Lumpur")
        start = date_from.replace(tzinfo=local_zone) if date_from.tzinfo is None else date_from
        end = date_to.replace(tzinfo=local_zone) if date_to.tzinfo is None else date_to
        if start.astimezone(timezone.utc) > end.astimezone(timezone.utc):
            raise HTTPException(400, "Start date must not be after end date")
    service = MonitoringQueryService(db)
    event_page = service.events(tab="attention" if tab == "attention" else "all", page=page,
        category=category, event_type=event_type, severity=severity,
        site_id=site, camera_id=camera, search=search, operator=operator,
        date_from=date_from, date_to=date_to) if tab in {"attention", "all"} else None
    active_alerts = service.alerts()
    context = _context(request, db, user, "events")
    context.update({
        "tab": tab,
        "event_page": event_page,
        "alerts": service.alerts(status="resolved") if tab == "history" else active_alerts if tab == "alerts" else (),
        "center_summary": service.event_center_summary(),
        "active_alert_count": len(active_alerts),
        "event_alerts": service.alerts_for_events(event_page.items) if event_page else {},
        "correlation_counts": service.correlation_counts(event_page.items) if event_page else {},
        "sites": service.sites(), "cameras": service.cameras(), "event_types": service.event_types(),
        "filters": {"category": category, "event_type": event_type, "severity": severity,
                    "site": str(site or ""), "camera": str(camera or ""), "search": search,
                    "operator": operator,
                    "date_from": date_from.strftime("%Y-%m-%dT%H:%M") if date_from else "",
                    "date_to": date_to.strftime("%Y-%m-%dT%H:%M") if date_to else ""},
        "display_time": service.display_time, "source_url": service.source_url,
        "category_label": service.category_label, "category_icon": service.category_icon,
        "date_group": service.date_group, "event_context": service.event_context,
        "source_action": service.source_action, "is_major": service.is_major,
        "event_video_url": service.event_video_url,
        "can_view_media": user_has_permission(user, "media.view"),
        "poll_after": datetime.now(timezone.utc).isoformat(),
    })
    return templates.TemplateResponse(request=request, name="monitoring/events.html", context=context)


@router.get("/events/{event_id}", response_class=HTMLResponse)
def event_detail(event_id: UUID, request: Request,
    user: User = Depends(require_page_permission("monitoring.view")), db: Session = Depends(get_db)) -> HTMLResponse:
    event = db.scalar(select(UnifiedEvent).options(joinedload(UnifiedEvent.site), joinedload(UnifiedEvent.camera)).where(UnifiedEvent.id == event_id))
    if event is None:
        raise HTTPException(404, "Operational event not found")
    service = MonitoringQueryService(db)
    context = _context(request, db, user, "events")
    context.update({"event": event, "related": service.correlated(event),
                    "display_time": service.display_time, "source_url": service.source_url,
                    "category_label": service.category_label})
    return templates.TemplateResponse(request=request, name="monitoring/event_detail.html", context=context)


@router.get("/alerts/{alert_id}", response_class=HTMLResponse)
def alert_detail(alert_id: UUID, request: Request,
    user: User = Depends(require_page_permission("monitoring.view")), db: Session = Depends(get_db)) -> HTMLResponse:
    alert = db.scalar(select(OperationalAlert).options(joinedload(OperationalAlert.site), joinedload(OperationalAlert.acknowledged_by)).where(OperationalAlert.id == alert_id))
    if alert is None:
        raise HTTPException(404, "Operational alert not found")
    health = db.scalar(select(ComponentHealth).where(ComponentHealth.component_type == alert.component_type, ComponentHealth.component_id == alert.component_id))
    service = MonitoringQueryService(db)
    context = _context(request, db, user, "events")
    context.update({"alert": alert, "health": health, "display_time": service.display_time})
    return templates.TemplateResponse(request=request, name="monitoring/alert_detail.html", context=context)


@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: UUID, request: Request, note: Annotated[str, Form()],
    user: User = Depends(require_page_permission("alerts.acknowledge")),
    _csrf: str = Depends(require_session_csrf), db: Session = Depends(get_db)) -> RedirectResponse:
    alert = db.get(OperationalAlert, alert_id)
    if alert is None:
        raise HTTPException(404, "Operational alert not found")
    try:
        AlertService(db).acknowledge(alert, actor_user_id=user.id, note=note,
            ip_address=request.client.host if request.client else None)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    db.commit()
    return RedirectResponse(f"/alerts/{alert.id}", status_code=303)


@router.get("/system-health", response_class=HTMLResponse)
def system_health(request: Request,
    user: User = Depends(require_page_permission("monitoring.view")), db: Session = Depends(get_db)) -> HTMLResponse:
    service = MonitoringQueryService(db)
    groups = service.health_groups()
    active_alerts = service.alerts()
    all_components = tuple(item for items in groups.values() for item in items)
    attention_components = tuple(sorted(
        (item for item in all_components if item.health_state in {"FAULT", "DEGRADED"}),
        key=lambda item: (0 if item.health_state == "FAULT" else 1, item.display_name),
    ))
    last_updated = max((item.observed_at for item in all_components), default=None)
    context = _context(request, db, user, "system-health")
    context.update({"groups": groups, "attention_components": attention_components,
                    "health_counts": service.health_counts(),
                    "site_health": service.site_health_summaries(),
                    "active_alerts": active_alerts, "component_alerts": service.alert_map(active_alerts),
                    "display_time": service.display_time, "relative_time": service.relative_time,
                    "state_age": service.state_age, "component_summary": service.component_summary,
                    "component_action": service.component_action, "last_updated": last_updated,
                    "monitor_interval": settings.monitor_interval_seconds})
    return templates.TemplateResponse(request=request, name="monitoring/health.html", context=context)


@router.get("/system-health/{component_type}/{component_id}", response_class=HTMLResponse)
def health_detail(component_type: str, component_id: str, request: Request,
    user: User = Depends(require_page_permission("monitoring.view")), db: Session = Depends(get_db)) -> HTMLResponse:
    item = db.scalar(select(ComponentHealth).options(joinedload(ComponentHealth.site)).where(
        ComponentHealth.component_type == component_type, ComponentHealth.component_id == component_id))
    if item is None:
        raise HTTPException(404, "Health component not found")
    alerts = tuple(db.scalars(select(OperationalAlert).where(
        OperationalAlert.component_type == component_type, OperationalAlert.component_id == component_id)
        .order_by(OperationalAlert.last_seen_at.desc())).all())
    service = MonitoringQueryService(db)
    context = _context(request, db, user, "system-health")
    context.update({"component": item, "alerts": alerts, "display_time": service.display_time})
    return templates.TemplateResponse(request=request, name="monitoring/health_detail.html", context=context)


@router.get("/api/monitoring/events", response_class=JSONResponse)
def incremental_events(after: datetime, attention_only: bool = False,
    _user: User = Depends(require_permission("monitoring.view")),
    db: Session = Depends(get_db)) -> dict[str, object]:
    service = MonitoringQueryService(db)
    page = service.events(tab="attention" if attention_only else "all", after=after, page_size=50)
    return {"items": [{"id": str(item.id), "event_type": item.event_type, "severity": item.severity,
                       "attention_required": item.attention_required, "occurred_at": item.occurred_at.isoformat(),
                       "subject": item.display_subject, "summary": item.summary} for item in reversed(page.items)],
            "count": page.total}


@router.get("/api/monitoring/status", response_class=JSONResponse)
def monitoring_status(_user: User = Depends(require_permission("monitoring.view")), db: Session = Depends(get_db)) -> dict[str, object]:
    service = MonitoringQueryService(db)
    return {"health": service.health_counts(), "active_alerts": len(service.alerts())}
