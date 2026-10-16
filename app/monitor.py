from __future__ import annotations

import logging
import signal
import threading
from datetime import datetime, timezone
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.health_monitor_service import MonitoringService
from app.services.health_monitor_service import ComponentHealthService


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("anpr.monitor")


def main() -> None:
    stop = threading.Event()

    def request_stop(*_args: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    logger.info("ANPR monitoring worker started; interval=%ss", settings.monitor_interval_seconds)
    consecutive_database_failures = 0
    database_outage_started: datetime | None = None
    while not stop.is_set():
        try:
            with SessionLocal() as db:
                if database_outage_started is not None:
                    ComponentHealthService(db).observe(component_type="DATABASE", component_id="postgresql",
                        display_name="PostgreSQL", state="FAULT",
                        summary="Database was unavailable to the monitoring worker",
                        observed_at=database_outage_started, capabilities={"claim": "VERIFIED"})
                result = MonitoringService(db).run_once()
            logger.info("Monitoring cycle complete: %s", result)
            consecutive_database_failures = 0
            database_outage_started = None
        except SQLAlchemyError:
            consecutive_database_failures += 1
            if consecutive_database_failures >= settings.monitor_database_failure_debounce_count and database_outage_started is None:
                database_outage_started = datetime.now(timezone.utc)
            logger.exception("Monitoring cycle failed because PostgreSQL was unavailable")
        except Exception:
            logger.exception("Monitoring cycle failed")
        stop.wait(settings.monitor_interval_seconds)
    logger.info("ANPR monitoring worker stopped")


if __name__ == "__main__":
    main()
