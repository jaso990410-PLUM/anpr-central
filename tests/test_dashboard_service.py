from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.camera import Camera
from app.models.site import Site
from app.models.user import User
from app.models.user_session import UserSession
from app.services.dashboard_service import get_dashboard_summary


NOW = datetime(2026, 9, 27, 7, 0, tzinfo=timezone.utc)


def test_dashboard_summary_uses_live_database_counts(db: Session) -> None:
    main_site = Site(code="MAIN", name="Main Site", is_active=True)
    standby_site = Site(code="STANDBY", name="Standby", is_active=False)
    active_user = User(
        username="active-user",
        full_name="Active User",
        password_hash="unused",
        is_active=True,
    )
    inactive_user = User(
        username="inactive-user",
        full_name="Inactive User",
        password_hash="unused",
        is_active=False,
    )
    db.add_all([main_site, standby_site, active_user, inactive_user])
    db.flush()

    db.add_all(
        [
            Camera(
                site_id=main_site.id,
                code="MAIN-ENTRY-01",
                name="Main Entry",
                direction="entry",
                ip_address="192.0.2.10",
                is_enabled=True,
                last_seen_at=NOW,
            ),
            Camera(
                site_id=main_site.id,
                code="MAIN-EXIT-01",
                name="Main Exit",
                direction="exit",
                is_enabled=False,
            ),
            UserSession(
                user_id=active_user.id,
                token_hash="a" * 64,
                created_at=NOW - timedelta(minutes=5),
                last_seen_at=NOW - timedelta(minutes=1),
                expires_at=NOW + timedelta(hours=1),
            ),
            UserSession(
                user_id=active_user.id,
                token_hash="b" * 64,
                created_at=NOW - timedelta(minutes=5),
                last_seen_at=NOW - timedelta(minutes=2),
                expires_at=NOW + timedelta(hours=1),
            ),
            UserSession(
                user_id=inactive_user.id,
                token_hash="c" * 64,
                created_at=NOW - timedelta(minutes=5),
                last_seen_at=NOW - timedelta(minutes=1),
                expires_at=NOW + timedelta(hours=1),
            ),
        ]
    )
    db.flush()

    summary = get_dashboard_summary(db, now=NOW)

    assert summary.site_count == 2
    assert summary.active_site_count == 1
    assert summary.site_context == "MAIN"
    assert summary.site is not None
    assert summary.site.name == "Main Site"
    assert summary.site.timezone == "Asia/Kuala_Lumpur"
    assert summary.site.is_active
    assert summary.database_connected
    assert summary.application_name == "ANPR Central"
    assert summary.application_version == "0.1.0"
    assert summary.camera_count == 2
    assert summary.enabled_camera_count == 1
    assert summary.cameras[0].code == "MAIN-ENTRY-01"
    assert summary.cameras[0].ip_address == "192.0.2.10"
    assert summary.cameras[0].last_seen_display == "2026-09-27 15:00:00"
    assert summary.user_count == 2
    assert summary.active_user_count == 1
    assert summary.active_session_count == 2


def test_dashboard_uses_configured_default_site(db: Session) -> None:
    db.add_all(
        [
            Site(code="A-SITE", name="Alternate Site", is_active=True),
            Site(code="MAIN", name="Main Site", is_active=True),
        ]
    )
    db.flush()

    summary = get_dashboard_summary(db, now=NOW)

    assert summary.site_count == 2
    assert summary.active_site_count == 2
    assert summary.site_context == "MAIN"
    assert summary.site is not None
    assert summary.site.name == "Main Site"
