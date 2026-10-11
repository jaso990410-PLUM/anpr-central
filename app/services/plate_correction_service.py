from __future__ import annotations

from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import aliased
from sqlalchemy.orm import Session

from app.models.operations import RecognitionPlateCorrection
from app.models.recognition_event import RecognitionEvent
from app.services.access_list_service import AccessListValidationError, normalize_plate


class PlateCorrectionError(ValueError):
    pass


class PlateCorrectionService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def latest(self, event_id: UUID) -> RecognitionPlateCorrection | None:
        newer = aliased(RecognitionPlateCorrection)
        return self.db.scalar(
            select(RecognitionPlateCorrection)
            .where(
                RecognitionPlateCorrection.recognition_event_id == event_id,
                ~exists(select(newer.id).where(newer.supersedes_correction_id == RecognitionPlateCorrection.id)),
            )
            .order_by(RecognitionPlateCorrection.created_at.desc(), RecognitionPlateCorrection.id.desc())
            .limit(1)
        )

    def history(self, event_id: UUID) -> tuple[RecognitionPlateCorrection, ...]:
        current = self.latest(event_id)
        result = []
        while current is not None:
            result.append(current)
            current = self.db.get(RecognitionPlateCorrection, current.supersedes_correction_id) if current.supersedes_correction_id else None
        return tuple(result)

    def correct(self, event_id: UUID, *, corrected_plate: str, reason: str, user_id: UUID) -> RecognitionPlateCorrection:
        event = self.db.get(RecognitionEvent, event_id)
        if event is None:
            raise PlateCorrectionError("Recognition event was not found.")
        clean_reason = reason.strip()
        if len(clean_reason) < 3:
            raise PlateCorrectionError("Enter a meaningful correction reason.")
        try:
            normalized = normalize_plate(corrected_plate)
        except AccessListValidationError as error:
            raise PlateCorrectionError(str(error)) from error
        previous = self.latest(event.id)
        correction = RecognitionPlateCorrection(
            recognition_event_id=event.id,
            corrected_plate=corrected_plate.strip().upper(),
            corrected_plate_normalized=normalized,
            reason=clean_reason[:500],
            created_by_user_id=user_id,
            supersedes_correction_id=previous.id if previous else None,
        )
        self.db.add(correction)
        self.db.flush()
        return correction


def effective_plate(db: Session, event: RecognitionEvent) -> str:
    correction = PlateCorrectionService(db).latest(event.id)
    if correction is not None:
        return correction.corrected_plate_normalized
    try:
        return normalize_plate(event.plate_text)
    except AccessListValidationError:
        return event.plate_text.strip().upper()
