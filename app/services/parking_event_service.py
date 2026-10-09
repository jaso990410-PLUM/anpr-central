from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.models.recognition_event import RecognitionEvent
from app.services.parking_policy_service import DecisionContextBuilder, create_access_decision
from app.services.parking_session_service import ParkingSessionService
from app.services.parking_review_service import ParkingReviewService

logger = logging.getLogger(__name__)


class ParkingEventCoordinator:
    """Persist the business result first; invoke hardware only after commit."""

    def __init__(self, session_factory: sessionmaker[Session], action_callback=None):
        self._session_factory = session_factory
        self._action_callback = action_callback

    def process_event(self, event_id: UUID) -> None:
        decision_id = None
        with self._session_factory() as db:
            event = db.get(RecognitionEvent, event_id)
            if event is None:
                return
            context = DecisionContextBuilder(db).build(event)
            decision = create_access_decision(db, context)
            parking_session = ParkingSessionService(db).apply(context, decision.decision)
            ParkingReviewService(db).apply_exit_result(context, decision, parking_session)
            decision_id = decision.id
            db.commit()
            is_live = event.source == "LIVE"
        if is_live and self._action_callback is not None and decision_id is not None:
            try:
                self._action_callback(decision_id)
            except Exception:
                logger.exception("Decision action failed after durable commit", extra={"decision_id": str(decision_id)})
