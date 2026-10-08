from __future__ import annotations

from datetime import datetime, timezone
from threading import Event, Thread

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.parking_session_service import scan_overstays


class StayMonitorService:
    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="anpr-stay-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None

    def scan_once(self) -> int:
        with self._session_factory() as db:
            count = scan_overstays(db, now=datetime.now(timezone.utc))
            db.commit()
            return count

    def _run(self) -> None:
        while not self._stop.wait(settings.stay_monitor_interval_seconds):
            try:
                self.scan_once()
            except Exception:
                pass


stay_monitor_service = StayMonitorService()
