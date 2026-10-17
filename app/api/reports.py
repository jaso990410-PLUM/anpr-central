from __future__ import annotations

import csv
from io import StringIO
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BeforeValidator
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.db.dependencies import get_db
from app.dependencies.auth import require_page_permission, require_permission, user_has_permission
from app.models.user import User
from app.services.audit_service import create_audit_log
from app.services.dashboard_service import get_dashboard_summary
from app.services.reporting_service import ReportQuery, ReportingService, ReportValidationError, export_rows
from app.services.shell_service import build_navigation, role_label
from app.web import templates


router = APIRouter(prefix="/reports")


def _blank(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


OptionalUUID = Annotated[UUID | None, BeforeValidator(_blank), Query()]


def _query(
    service: ReportingService,
    *,
    site: UUID | None,
    period: str,
    date_from: str,
    date_to: str,
    camera: UUID | None,
) -> ReportQuery:
    try:
        return service.query(site_id=site, period=period, date_from=date_from,
                             date_to=date_to, camera_id=camera)
    except ReportValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _context(request: Request, db: Session, user: User, query: ReportQuery, active: str) -> dict[str, object]:
    service = ReportingService(db)
    return {
        "request": request,
        "user": user,
        "csrf_token": generate_csrf_token(request.cookies[settings.session_cookie_name]),
        "summary": get_dashboard_summary(db),
        "active_page": "reports",
        "navigation": build_navigation(user),
        "user_role_label": role_label(user),
        "report_active": active,
        "report_query": query,
        "report_params": urlencode(query.params()),
        "sites": service.sites(),
        "cameras": service.cameras(query.site.id),
        "can_export_reports": user_has_permission(user, "reports.export"),
    }


def _domain_data(service: ReportingService, domain: str, query: ReportQuery) -> dict[str, object]:
    loaders = {
        "traffic": service.traffic,
        "access": service.access,
        "operations": service.operations,
        "reliability": service.reliability,
    }
    try:
        return loaders[domain](query)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Report not found") from error


@router.get("", response_class=HTMLResponse)
def reports_landing(
    request: Request,
    site: OptionalUUID = None,
    period: str = "last_7_days",
    date_from: str = "",
    date_to: str = "",
    camera: OptionalUUID = None,
    user: User = Depends(require_page_permission("reports.view")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    service = ReportingService(db)
    normalized = _query(service, site=site, period=period, date_from=date_from,
                        date_to=date_to, camera=camera)
    context = _context(request, db, user, normalized, "overview")
    context["analytics"] = service.landing(normalized)
    return templates.TemplateResponse(request=request, name="reports/index.html", context=context)


def _report_page(
    domain: str,
    request: Request,
    site: UUID | None,
    period: str,
    date_from: str,
    date_to: str,
    camera: UUID | None,
    user: User,
    db: Session,
) -> HTMLResponse:
    service = ReportingService(db)
    normalized = _query(service, site=site, period=period, date_from=date_from,
                        date_to=date_to, camera=camera)
    context = _context(request, db, user, normalized, domain)
    context["analytics"] = _domain_data(service, domain, normalized)
    return templates.TemplateResponse(request=request, name=f"reports/{domain}.html", context=context)


@router.get("/traffic", response_class=HTMLResponse)
def traffic_report(request: Request, site: OptionalUUID = None, period: str = "last_7_days",
                   date_from: str = "", date_to: str = "", camera: OptionalUUID = None,
                   user: User = Depends(require_page_permission("reports.view")),
                   db: Session = Depends(get_db)) -> HTMLResponse:
    return _report_page("traffic", request, site, period, date_from, date_to, camera, user, db)


@router.get("/access", response_class=HTMLResponse)
def access_report(request: Request, site: OptionalUUID = None, period: str = "last_7_days",
                  date_from: str = "", date_to: str = "", camera: OptionalUUID = None,
                  user: User = Depends(require_page_permission("reports.view")),
                  db: Session = Depends(get_db)) -> HTMLResponse:
    return _report_page("access", request, site, period, date_from, date_to, camera, user, db)


@router.get("/operations", response_class=HTMLResponse)
def operations_report(request: Request, site: OptionalUUID = None, period: str = "last_7_days",
                      date_from: str = "", date_to: str = "", camera: OptionalUUID = None,
                      user: User = Depends(require_page_permission("reports.view")),
                      db: Session = Depends(get_db)) -> HTMLResponse:
    return _report_page("operations", request, site, period, date_from, date_to, camera, user, db)


@router.get("/reliability", response_class=HTMLResponse)
def reliability_report(request: Request, site: OptionalUUID = None, period: str = "last_30_days",
                       date_from: str = "", date_to: str = "", camera: OptionalUUID = None,
                       user: User = Depends(require_page_permission("reports.view")),
                       db: Session = Depends(get_db)) -> HTMLResponse:
    return _report_page("reliability", request, site, period, date_from, date_to, camera, user, db)


@router.get("/{domain}/export.csv")
def export_report(
    domain: str,
    request: Request,
    site: OptionalUUID = None,
    period: str = "last_7_days",
    date_from: str = "",
    date_to: str = "",
    camera: OptionalUUID = None,
    user: User = Depends(require_permission("reports.export")),
    db: Session = Depends(get_db),
) -> Response:
    if domain not in {"traffic", "access", "operations", "reliability"}:
        raise HTTPException(status_code=404, detail="Report not found")
    service = ReportingService(db)
    normalized = _query(service, site=site, period=period, date_from=date_from,
                        date_to=date_to, camera=camera)
    data = _domain_data(service, domain, normalized)
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("Report", domain.replace("_", " ").title()))
    writer.writerow(("Site", f"{normalized.site.code} · {normalized.site.name}"))
    writer.writerow(("Period", normalized.range_label))
    writer.writerow(("Timezone", normalized.timezone_name))
    writer.writerow(("Lane / Camera", normalized.camera.code if normalized.camera else "All lanes"))
    writer.writerow(())
    writer.writerow(("Section", "Metric", "Period / Bucket", "Value"))
    writer.writerows(export_rows(domain, data))
    create_audit_log(
        db,
        action="report.export",
        actor_user_id=user.id,
        site_id=normalized.site.id,
        entity_type="report",
        entity_id=domain,
        details={"report": domain, "date_from": normalized.date_from.isoformat(),
                 "date_to": normalized.date_to.isoformat(),
                 "camera": normalized.camera.code if normalized.camera else "all"},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    content = "\ufeff" + output.getvalue()
    filename = f"anpr-{domain}-{normalized.site.code}-{normalized.date_from}-{normalized.date_to}.csv"
    return Response(content=content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
