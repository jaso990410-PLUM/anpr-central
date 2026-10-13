from __future__ import annotations
import logging
import signal
from app.core.config import settings
from app.db.session import SessionLocal
from app.services.recorder_service import RecorderService


def main() -> None:
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s %(message)s")
    service=RecorderService(SessionLocal); stopped=False
    def stop(*_):
        nonlocal stopped; stopped=True; service.stop()
    signal.signal(signal.SIGINT,stop); signal.signal(signal.SIGTERM,stop); service.start()
    while not stopped:
        service.stop_event.wait(settings.media_recorder_poll_seconds)
        if stopped: break
        service.sync_workers(); service.maintenance_once()


if __name__ == "__main__": main()
